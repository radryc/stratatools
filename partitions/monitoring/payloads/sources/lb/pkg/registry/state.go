package registry

import (
	"fmt"
	"sort"
	"sync"

	"github.com/rydzu/ainfra/lb/pkg/model"
	"github.com/rydzu/ainfra/lb/pkg/pb"
)

const defaultAccount = "default"

type backendKey struct {
	ip   string
	port int32
}

type serviceRecord struct {
	account      string
	serviceName  string
	protocol     string
	description  string
	externalPort int32
	backends     map[backendKey]struct{}
}

type State struct {
	mu sync.RWMutex

	minExternalPort int32
	maxExternalPort int32

	services       map[string]*serviceRecord
	usedPorts      map[int32]string
	nextAllocProbe int32
}

func NewState(minExternalPort, maxExternalPort int32) *State {
	if minExternalPort <= 0 {
		minExternalPort = 10000
	}
	if maxExternalPort < minExternalPort {
		maxExternalPort = 60000
	}
	return &State{
		minExternalPort: minExternalPort,
		maxExternalPort: maxExternalPort,
		services:        make(map[string]*serviceRecord),
		usedPorts:       make(map[int32]string),
		nextAllocProbe:  minExternalPort,
	}
}

func serviceKey(account, serviceName string) string {
	if account == "" {
		account = defaultAccount
	}
	return account + "/" + serviceName
}

func (s *State) Register(req *pb.RegisterServiceRequest) (int32, error) {
	if req.GetServiceName() == "" {
		return 0, fmt.Errorf("service_name is required")
	}
	if req.GetTargetIp() == "" {
		return 0, fmt.Errorf("target_ip is required")
	}
	if req.GetTargetPort() <= 0 {
		return 0, fmt.Errorf("target_port must be > 0")
	}

	acc := req.GetAccount()
	if acc == "" {
		acc = defaultAccount
	}
	key := serviceKey(acc, req.GetServiceName())
	bk := backendKey{ip: req.GetTargetIp(), port: req.GetTargetPort()}

	s.mu.Lock()
	defer s.mu.Unlock()

	rec, ok := s.services[key]
	if !ok {
		externalPort, err := s.reservePortLocked(key, req.GetRequestedExternalPort())
		if err != nil {
			return 0, err
		}
		rec = &serviceRecord{
			account:      acc,
			serviceName:  req.GetServiceName(),
			externalPort: externalPort,
			backends:     make(map[backendKey]struct{}),
		}
		s.services[key] = rec
	}
	if req.GetRequestedExternalPort() > 0 && req.GetRequestedExternalPort() != rec.externalPort {
		return 0, fmt.Errorf("service %q already pinned to external port %d", key, rec.externalPort)
	}

	rec.backends[bk] = struct{}{}
	return rec.externalPort, nil
}

// Seed inserts a bootstrap backend directly, bypassing gRPC proto constraints.
// It sets protocol and description on first creation; subsequent calls for the
// same service key only add backends.
func (s *State) Seed(account, serviceName, protocol, description string, externalPort int32, targetIP string, targetPort int32) error {
	if serviceName == "" {
		return fmt.Errorf("service_name is required")
	}
	if targetIP == "" {
		return fmt.Errorf("target_ip is required")
	}
	if targetPort <= 0 {
		return fmt.Errorf("target_port must be > 0")
	}
	if account == "" {
		account = defaultAccount
	}
	key := serviceKey(account, serviceName)
	bk := backendKey{ip: targetIP, port: targetPort}

	s.mu.Lock()
	defer s.mu.Unlock()

	rec, ok := s.services[key]
	if !ok {
		port, err := s.reservePortLocked(key, externalPort)
		if err != nil {
			return err
		}
		rec = &serviceRecord{
			account:      account,
			serviceName:  serviceName,
			protocol:     protocol,
			description:  description,
			externalPort: port,
			backends:     make(map[backendKey]struct{}),
		}
		s.services[key] = rec
	}
	if externalPort > 0 && externalPort != rec.externalPort {
		return fmt.Errorf("service %q already pinned to external port %d", key, rec.externalPort)
	}
	rec.backends[bk] = struct{}{}
	return nil
}

func (s *State) Deregister(req *pb.DeregisterServiceRequest) (bool, error) {
	if req.GetServiceName() == "" {
		return false, fmt.Errorf("service_name is required")
	}
	if req.GetTargetIp() == "" {
		return false, fmt.Errorf("target_ip is required")
	}

	acc := req.GetAccount()
	if acc == "" {
		acc = defaultAccount
	}
	key := serviceKey(acc, req.GetServiceName())

	s.mu.Lock()
	defer s.mu.Unlock()

	rec, ok := s.services[key]
	if !ok {
		return false, nil
	}

	removed := false
	if req.GetTargetPort() > 0 {
		bk := backendKey{ip: req.GetTargetIp(), port: req.GetTargetPort()}
		if _, found := rec.backends[bk]; found {
			delete(rec.backends, bk)
			removed = true
		}
	} else {
		for bk := range rec.backends {
			if bk.ip == req.GetTargetIp() {
				delete(rec.backends, bk)
				removed = true
			}
		}
	}

	if len(rec.backends) == 0 {
		delete(s.services, key)
		delete(s.usedPorts, rec.externalPort)
		if rec.externalPort < s.nextAllocProbe {
			s.nextAllocProbe = rec.externalPort
		}
	}
	return removed, nil
}

func (s *State) Snapshot() model.Snapshot {
	s.mu.RLock()
	defer s.mu.RUnlock()

	services := make([]model.ServiceRoute, 0, len(s.services))
	for _, rec := range s.services {
		backends := make([]model.Backend, 0, len(rec.backends))
		for bk := range rec.backends {
			backends = append(backends, model.Backend{TargetIP: bk.ip, TargetPort: bk.port})
		}
		sort.Slice(backends, func(i, j int) bool {
			if backends[i].TargetIP == backends[j].TargetIP {
				return backends[i].TargetPort < backends[j].TargetPort
			}
			return backends[i].TargetIP < backends[j].TargetIP
		})
		services = append(services, model.ServiceRoute{
			Account:      rec.account,
			ServiceName:  rec.serviceName,
			Protocol:     rec.protocol,
			Description:  rec.description,
			ExternalPort: rec.externalPort,
			Backends:     backends,
		})
	}
	sort.Slice(services, func(i, j int) bool {
		if services[i].Account == services[j].Account {
			if services[i].ServiceName == services[j].ServiceName {
				return services[i].ExternalPort < services[j].ExternalPort
			}
			return services[i].ServiceName < services[j].ServiceName
		}
		return services[i].Account < services[j].Account
	})

	return model.Snapshot{Services: services}
}

func (s *State) ListResponse() *pb.ListServicesResponse {
	snap := s.Snapshot()
	services := make([]*pb.ServiceRoute, 0, len(snap.Services))
	for _, svc := range snap.Services {
		backends := make([]*pb.Backend, 0, len(svc.Backends))
		for _, b := range svc.Backends {
			backends = append(backends, &pb.Backend{TargetIp: b.TargetIP, TargetPort: b.TargetPort})
		}
		services = append(services, &pb.ServiceRoute{
			Account:      svc.Account,
			ServiceName:  svc.ServiceName,
			ExternalPort: svc.ExternalPort,
			Backends:     backends,
		})
	}
	return &pb.ListServicesResponse{Services: services}
}

func (s *State) reservePortLocked(serviceKey string, requested int32) (int32, error) {
	if requested > 0 {
		if requested <= 0 || requested > 65535 {
			return 0, fmt.Errorf("requested_external_port %d outside [1, 65535]", requested)
		}
		if owner, ok := s.usedPorts[requested]; ok && owner != serviceKey {
			return 0, fmt.Errorf("requested_external_port %d already reserved by %q", requested, owner)
		}
		s.usedPorts[requested] = serviceKey
		return requested, nil
	}

	for port := s.nextAllocProbe; port <= s.maxExternalPort; port++ {
		if _, used := s.usedPorts[port]; !used {
			s.usedPorts[port] = serviceKey
			s.nextAllocProbe = port + 1
			return port, nil
		}
	}
	for port := s.minExternalPort; port < s.nextAllocProbe; port++ {
		if _, used := s.usedPorts[port]; !used {
			s.usedPorts[port] = serviceKey
			s.nextAllocProbe = port + 1
			return port, nil
		}
	}
	return 0, fmt.Errorf("no external ports available in [%d, %d]", s.minExternalPort, s.maxExternalPort)
}
