package telemetry

import (
	"context"
	"fmt"
	"net/http"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/prometheus"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
)

type Runtime struct {
	Provider *sdkmetric.MeterProvider
	Meter    metric.Meter
}

func InitMetrics(ctx context.Context, addr string) (*Runtime, error) {
	exporter, err := prometheus.New()
	if err != nil {
		return nil, fmt.Errorf("create prometheus exporter: %w", err)
	}

	provider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(exporter))
	otel.SetMeterProvider(provider)

	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	go func() {
		_ = http.ListenAndServe(addr, mux)
	}()

	return &Runtime{
		Provider: provider,
		Meter:    provider.Meter("lb-proxy"),
	}, nil
}

func (r *Runtime) Shutdown(ctx context.Context) error {
	if r == nil || r.Provider == nil {
		return nil
	}
	return r.Provider.Shutdown(ctx)
}
