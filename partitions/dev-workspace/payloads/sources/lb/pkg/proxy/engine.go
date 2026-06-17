package proxy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/rydzu/ainfra/lb/pkg/model"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

type ProxyMetrics struct {
	activeConnections   metric.Int64UpDownCounter
	connectionErrors    metric.Int64Counter
	circuitBreakerTrips metric.Int64Counter
}

func NewProxyMetrics(meter metric.Meter) (*ProxyMetrics, error) {
	active, err := meter.Int64UpDownCounter("proxy_active_connections")
	if err != nil {
		return nil, err
	}
	errs, err := meter.Int64Counter("proxy_connection_errors_total")
	if err != nil {
		return nil, err
	}
	trips, err := meter.Int64Counter("proxy_circuit_breaker_trips_total")
	if err != nil {
		return nil, err
	}
	return &ProxyMetrics{
		activeConnections:   active,
		connectionErrors:    errs,
		circuitBreakerTrips: trips,
	}, nil
}

func (m *ProxyMetrics) attrs(backendIP string, externalPort int32) metric.AddOption {
	return metric.WithAttributes(attribute.String("backend_ip", backendIP), attribute.Int("external_port", int(externalPort)))
}

func (m *ProxyMetrics) addActive(ctx context.Context, delta int64, backendIP string, externalPort int32) {
	if m == nil {
		return
	}
	m.activeConnections.Add(ctx, delta, m.attrs(backendIP, externalPort))
}

func (m *ProxyMetrics) addError(ctx context.Context, backendIP string, externalPort int32) {
	if m == nil {
		return
	}
	m.connectionErrors.Add(ctx, 1, m.attrs(backendIP, externalPort))
}

func (m *ProxyMetrics) addTrip(ctx context.Context, backendIP string, externalPort int32) {
	if m == nil {
		return
	}
	m.circuitBreakerTrips.Add(ctx, 1, m.attrs(backendIP, externalPort))
}

type listenerInstance struct {
	externalPort int32
	listener     net.Listener

	mu       sync.RWMutex
	backends map[string]*backend
	rr       atomic.Uint64
}

type Engine struct {
	bindAddress   string
	registryURL   string
	syncEvery     time.Duration
	dialTimeout   time.Duration
	httpClient    *http.Client
	metrics       *ProxyMetrics
	proxyProtocol bool

	mu        sync.RWMutex
	listeners map[int32]*listenerInstance
}

func NewEngine(bindAddress, registryURL string, syncEvery, dialTimeout time.Duration, metrics *ProxyMetrics, proxyProtocol bool) *Engine {
	if syncEvery <= 0 {
		syncEvery = 2 * time.Second
	}
	if dialTimeout <= 0 {
		dialTimeout = 3 * time.Second
	}
	return &Engine{
		bindAddress:   bindAddress,
		registryURL:   registryURL,
		syncEvery:     syncEvery,
		dialTimeout:   dialTimeout,
		httpClient:    &http.Client{Timeout: 5 * time.Second},
		metrics:       metrics,
		proxyProtocol: proxyProtocol,
		listeners:     make(map[int32]*listenerInstance),
	}
}

func (e *Engine) Run(ctx context.Context) error {
	if err := e.syncState(ctx); err != nil {
		log.Printf("initial sync failed: %v", err)
	}

	ticker := time.NewTicker(e.syncEvery)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			e.closeAllListeners()
			return nil
		case <-ticker.C:
			if err := e.syncState(ctx); err != nil {
				log.Printf("sync failed: %v", err)
			}
		}
	}
}

func (e *Engine) syncState(ctx context.Context) error {
	snapshot, err := e.fetchSnapshot(ctx)
	if err != nil {
		return err
	}

	desired := make(map[int32]map[string]model.Backend)
	for _, svc := range snapshot.Services {
		if svc.ExternalPort <= 0 {
			continue
		}
		if _, ok := desired[svc.ExternalPort]; !ok {
			desired[svc.ExternalPort] = make(map[string]model.Backend)
		}
		for _, b := range svc.Backends {
			k := backendMapKey(b.TargetIP, b.TargetPort)
			desired[svc.ExternalPort][k] = b
		}
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	for port := range desired {
		if _, ok := e.listeners[port]; !ok {
			li, err := e.startListenerLocked(port)
			if err != nil {
				log.Printf("listen on external port %d failed: %v", port, err)
				continue
			}
			e.listeners[port] = li
		}
	}

	for port, li := range e.listeners {
		d, ok := desired[port]
		if !ok {
			li.mu.Lock()
			for k, b := range li.backends {
				b.markDraining()
				if b.activeConns.Load() == 0 {
					delete(li.backends, k)
				}
			}
			empty := len(li.backends) == 0
			li.mu.Unlock()
			if empty {
				_ = li.listener.Close()
				delete(e.listeners, port)
			}
			continue
		}

		li.mu.Lock()
		for k, b := range li.backends {
			if _, keep := d[k]; !keep {
				b.markDraining()
				if b.activeConns.Load() == 0 {
					delete(li.backends, k)
				}
			}
		}
		for k, target := range d {
			if existing, found := li.backends[k]; found {
				if existing.stateValue() == stateDraining {
					existing.markActive()
				}
				continue
			}
			li.backends[k] = newBackend(target.TargetIP, target.TargetPort)
		}
		li.mu.Unlock()
	}

	return nil
}

func (e *Engine) startListenerLocked(port int32) (*listenerInstance, error) {
	addr := net.JoinHostPort(e.bindAddress, strconv.Itoa(int(port)))
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, err
	}
	li := &listenerInstance{
		externalPort: port,
		listener:     ln,
		backends:     make(map[string]*backend),
	}
	go e.acceptLoop(li)
	return li, nil
}

func (e *Engine) acceptLoop(li *listenerInstance) {
	for {
		conn, err := li.listener.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			var ne net.Error
			if errors.As(err, &ne) && ne.Temporary() {
				time.Sleep(50 * time.Millisecond)
				continue
			}
			return
		}
		go e.handleConn(li, conn)
	}
}

func (e *Engine) handleConn(li *listenerInstance, clientConn net.Conn) {
	defer clientConn.Close()

	b := li.pickBackend()
	if b == nil {
		return
	}

	b.activeConns.Add(1)
	e.metrics.addActive(context.Background(), 1, b.ip, li.externalPort)
	defer func() {
		remaining := b.activeConns.Add(-1)
		e.metrics.addActive(context.Background(), -1, b.ip, li.externalPort)
		if b.stateValue() == stateDraining && remaining == 0 {
			li.mu.Lock()
			if current, ok := li.backends[backendMapKey(b.ip, b.port)]; ok && current == b && b.activeConns.Load() == 0 {
				delete(li.backends, backendMapKey(b.ip, b.port))
			}
			li.mu.Unlock()
		}
	}()

	backendConn, err := net.DialTimeout("tcp", b.addr(), e.dialTimeout)
	if err != nil {
		e.metrics.addError(context.Background(), b.ip, li.externalPort)
		if b.consecFailure.Add(1) >= 3 {
			if b.markUnhealthy() {
				e.metrics.addTrip(context.Background(), b.ip, li.externalPort)
			}
		}
		return
	}
	defer backendConn.Close()

	b.consecFailure.Store(0)

	if e.proxyProtocol {
		if err := writeProxyProtocolV1(backendConn, clientConn); err != nil {
			e.metrics.addError(context.Background(), b.ip, li.externalPort)
			return
		}
	}

	proxyBidirectional(clientConn, backendConn)
}

func proxyBidirectional(clientConn, backendConn net.Conn) {
	done := make(chan struct{}, 2)
	go func() {
		_, _ = io.Copy(backendConn, clientConn)
		if tcp, ok := backendConn.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
		done <- struct{}{}
	}()
	go func() {
		_, _ = io.Copy(clientConn, backendConn)
		if tcp, ok := clientConn.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
		done <- struct{}{}
	}()
	<-done
	<-done
}

func (li *listenerInstance) pickBackend() *backend {
	li.mu.RLock()
	defer li.mu.RUnlock()

	if len(li.backends) == 0 {
		return nil
	}

	eligible := make([]*backend, 0, len(li.backends))
	for _, b := range li.backends {
		if b.stateValue() == stateActive {
			eligible = append(eligible, b)
		}
	}
	if len(eligible) == 0 {
		return nil
	}

	n := li.rr.Add(1)
	idx := int(n % uint64(len(eligible)))
	return eligible[idx]
}

func (e *Engine) fetchSnapshot(ctx context.Context) (model.Snapshot, error) {
	var snapshot model.Snapshot
	url := fmt.Sprintf("%s/services", e.registryURL)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return snapshot, err
	}
	resp, err := e.httpClient.Do(req)
	if err != nil {
		return snapshot, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return snapshot, fmt.Errorf("registry returned %s", resp.Status)
	}
	if err := json.NewDecoder(resp.Body).Decode(&snapshot); err != nil {
		return snapshot, err
	}
	return snapshot, nil
}

func backendMapKey(ip string, port int32) string {
	return ip + ":" + strconv.Itoa(int(port))
}

func (e *Engine) closeAllListeners() {
	e.mu.Lock()
	defer e.mu.Unlock()
	for port, li := range e.listeners {
		_ = li.listener.Close()
		delete(e.listeners, port)
	}
}
