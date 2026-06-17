package main

import (
	"context"
	"flag"
	"log"
	"os/signal"
	"syscall"
	"time"

	"github.com/rydzu/ainfra/lb/pkg/proxy"
	"github.com/rydzu/ainfra/lb/pkg/telemetry"
)

func main() {
	var (
		bindAddress   = flag.String("bind-address", "0.0.0.0", "listener bind address")
		registryURL   = flag.String("registry-url", "http://127.0.0.1:8081", "registry HTTP base URL")
		syncEvery     = flag.Duration("sync-every", 2*time.Second, "state sync interval")
		dialTimeout   = flag.Duration("dial-timeout", 3*time.Second, "backend dial timeout")
		metricsAddr   = flag.String("metrics-addr", ":9090", "Prometheus metrics listen address")
		proxyProtocol = flag.Bool("proxy-protocol", false, "send PROXY protocol v1 header to backends")
	)
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	tel, err := telemetry.InitMetrics(ctx, *metricsAddr)
	if err != nil {
		log.Fatalf("init telemetry: %v", err)
	}
	defer func() {
		_ = tel.Shutdown(context.Background())
	}()

	metrics, err := proxy.NewProxyMetrics(tel.Meter)
	if err != nil {
		log.Fatalf("create proxy metrics instruments: %v", err)
	}

	engine := proxy.NewEngine(*bindAddress, *registryURL, *syncEvery, *dialTimeout, metrics, *proxyProtocol)
	log.Printf("proxy syncing from %s and exposing metrics at %s", *registryURL, *metricsAddr)
	if err := engine.Run(ctx); err != nil {
		log.Fatalf("proxy exited: %v", err)
	}
}
