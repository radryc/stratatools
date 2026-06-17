package k8sagent

import (
	"context"
	"fmt"
	"log"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/rydzu/ainfra/lb/pkg/pb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/tools/clientcmd"
)

const (
	annotationExpose       = "guardian.intent/expose"
	annotationAccount      = "guardian.intent/account"
	annotationServiceName  = "guardian.intent/service-name"
	annotationExternalPort = "guardian.intent/external-port"
)

type Endpoint struct {
	IP   string
	Port int32
}

type Agent struct {
	client kubernetes.Interface
	rpc    pb.DiscoveryRegistryClient

	mu    sync.Mutex
	known map[string]map[string]Endpoint
}

func New(ctx context.Context, registryAddr string) (*Agent, error) {
	cfg, err := inClusterOrKubeConfig()
	if err != nil {
		return nil, err
	}
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return nil, err
	}

	conn, err := grpc.NewClient(registryAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, fmt.Errorf("connect registry grpc %q: %w", registryAddr, err)
	}

	return &Agent{
		client: client,
		rpc:    pb.NewDiscoveryRegistryClient(conn),
		known:  make(map[string]map[string]Endpoint),
	}, nil
}

func (a *Agent) Run(ctx context.Context, namespace string) error {
	factory := informers.NewSharedInformerFactoryWithOptions(a.client, 30*time.Second,
		informers.WithNamespace(namespace),
		informers.WithTweakListOptions(func(opts *metav1.ListOptions) {
			opts.FieldSelector = fields.Everything().String()
		}),
	)

	informer := factory.Core().V1().Endpoints().Informer()
	_, err := informer.AddEventHandler(cache.ResourceEventHandlerFuncs{
		AddFunc: func(obj interface{}) {
			a.reconcile(ctx, obj)
		},
		UpdateFunc: func(_, newObj interface{}) {
			a.reconcile(ctx, newObj)
		},
		DeleteFunc: func(obj interface{}) {
			a.handleDelete(ctx, obj)
		},
	})
	if err != nil {
		return err
	}

	factory.Start(ctx.Done())
	if !cache.WaitForCacheSync(ctx.Done(), informer.HasSynced) {
		return fmt.Errorf("k8s cache sync failed")
	}
	<-ctx.Done()
	return nil
}

func (a *Agent) reconcile(ctx context.Context, obj interface{}) {
	ep, ok := obj.(*corev1.Endpoints)
	if !ok {
		return
	}

	key := endpointResourceKey(ep.Namespace, ep.Name)
	account, serviceName, requestedExternalPort, exposed := a.resolveIntent(ep)
	if !exposed {
		a.deregisterAll(ctx, key, account, serviceName)
		return
	}

	current := parseEndpoints(ep)
	old := a.snapshotKnown(key)

	for epKey, entry := range current {
		if _, exists := old[epKey]; exists {
			continue
		}
		_, err := a.rpc.RegisterService(ctx, &pb.RegisterServiceRequest{
			Account:               account,
			ServiceName:           serviceName,
			TargetIp:              entry.IP,
			TargetPort:            entry.Port,
			RequestedExternalPort: requestedExternalPort,
		})
		if err != nil {
			log.Printf("register %s/%s %s:%d failed: %v", ep.Namespace, serviceName, entry.IP, entry.Port, err)
		}
	}

	for epKey, entry := range old {
		if _, exists := current[epKey]; exists {
			continue
		}
		_, err := a.rpc.DeregisterService(ctx, &pb.DeregisterServiceRequest{
			Account:     account,
			ServiceName: serviceName,
			TargetIp:    entry.IP,
			TargetPort:  entry.Port,
		})
		if err != nil {
			log.Printf("deregister %s/%s %s:%d failed: %v", ep.Namespace, serviceName, entry.IP, entry.Port, err)
		}
	}

	a.setKnown(key, current)
}

func (a *Agent) handleDelete(ctx context.Context, obj interface{}) {
	ep, ok := obj.(*corev1.Endpoints)
	if !ok {
		tombstone, ok := obj.(cache.DeletedFinalStateUnknown)
		if !ok {
			return
		}
		ep, ok = tombstone.Obj.(*corev1.Endpoints)
		if !ok {
			return
		}
	}
	account, serviceName, _, _ := a.resolveIntent(ep)
	a.deregisterAll(ctx, endpointResourceKey(ep.Namespace, ep.Name), account, serviceName)
}

func (a *Agent) deregisterAll(ctx context.Context, key, account, serviceName string) {
	old := a.snapshotKnown(key)
	for _, entry := range old {
		_, err := a.rpc.DeregisterService(ctx, &pb.DeregisterServiceRequest{
			Account:     account,
			ServiceName: serviceName,
			TargetIp:    entry.IP,
			TargetPort:  entry.Port,
		})
		if err != nil {
			log.Printf("deregister %s/%s %s:%d failed: %v", account, serviceName, entry.IP, entry.Port, err)
		}
	}
	a.clearKnown(key)
}

func (a *Agent) resolveIntent(ep *corev1.Endpoints) (account string, serviceName string, requestedExternalPort int32, exposed bool) {
	anns := ep.GetAnnotations()
	expose := strings.EqualFold(anns[annotationExpose], "true")

	account = anns[annotationAccount]
	if account == "" {
		account = ep.Namespace
	}

	serviceName = anns[annotationServiceName]
	if serviceName == "" {
		serviceName = ep.Name
	}

	if raw := anns[annotationExternalPort]; raw != "" {
		if v, err := strconv.Atoi(raw); err == nil {
			requestedExternalPort = int32(v)
		}
	}

	if !expose {
		if svc, err := a.client.CoreV1().Services(ep.Namespace).Get(context.Background(), ep.Name, metav1.GetOptions{}); err == nil {
			sanns := svc.GetAnnotations()
			if strings.EqualFold(sanns[annotationExpose], "true") {
				expose = true
				if account == ep.Namespace && sanns[annotationAccount] != "" {
					account = sanns[annotationAccount]
				}
				if serviceName == ep.Name && sanns[annotationServiceName] != "" {
					serviceName = sanns[annotationServiceName]
				}
				if requestedExternalPort == 0 && sanns[annotationExternalPort] != "" {
					if v, err := strconv.Atoi(sanns[annotationExternalPort]); err == nil {
						requestedExternalPort = int32(v)
					}
				}
			}
		}
	}

	return account, serviceName, requestedExternalPort, expose
}

func parseEndpoints(ep *corev1.Endpoints) map[string]Endpoint {
	out := make(map[string]Endpoint)
	for _, subset := range ep.Subsets {
		for _, addr := range subset.Addresses {
			for _, port := range subset.Ports {
				e := Endpoint{IP: addr.IP, Port: port.Port}
				out[endpointKey(e.IP, e.Port)] = e
			}
		}
	}
	return out
}

func endpointKey(ip string, port int32) string {
	return ip + ":" + strconv.Itoa(int(port))
}

func endpointResourceKey(namespace, name string) string {
	return namespace + "/" + name
}

func (a *Agent) snapshotKnown(key string) map[string]Endpoint {
	a.mu.Lock()
	defer a.mu.Unlock()
	current := a.known[key]
	copyMap := make(map[string]Endpoint, len(current))
	for k, v := range current {
		copyMap[k] = v
	}
	return copyMap
}

func (a *Agent) setKnown(key string, entries map[string]Endpoint) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.known[key] = entries
}

func (a *Agent) clearKnown(key string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	delete(a.known, key)
}

func inClusterOrKubeConfig() (*rest.Config, error) {
	cfg, err := rest.InClusterConfig()
	if err == nil {
		return cfg, nil
	}
	return clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
		clientcmd.NewDefaultClientConfigLoadingRules(),
		&clientcmd.ConfigOverrides{},
	).ClientConfig()
}
