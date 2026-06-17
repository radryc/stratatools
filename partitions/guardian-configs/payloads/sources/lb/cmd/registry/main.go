package main

import (
	"context"
	"flag"
	"log"
	"net"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	"github.com/rydzu/ainfra/lb/pkg/pb"
	"github.com/rydzu/ainfra/lb/pkg/registry"
	"google.golang.org/grpc"
)

func main() {
	var (
		grpcAddr = flag.String("grpc-addr", ":50051", "gRPC listen address")
		httpAddr = flag.String("http-addr", ":8081", "HTTP listen address")
		minPort  = flag.Int("min-external-port", 10000, "minimum dynamic external port")
		maxPort  = flag.Int("max-external-port", 60000, "maximum dynamic external port")
	)
	flag.Parse()

	state := registry.NewState(int32(*minPort), int32(*maxPort))
	grpcServer := grpc.NewServer()
	pb.RegisterDiscoveryRegistryServer(grpcServer, registry.NewGRPCServer(state))

	lis, err := net.Listen("tcp", *grpcAddr)
	if err != nil {
		log.Fatalf("listen grpc %s: %v", *grpcAddr, err)
	}

	httpSrv := &http.Server{
		Addr:    *httpAddr,
		Handler: registry.HTTPServicesHandler(state),
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Printf("registry gRPC listening on %s", *grpcAddr)
		if err := grpcServer.Serve(lis); err != nil {
			log.Printf("grpc serve stopped: %v", err)
		}
	}()

	go func() {
		log.Printf("registry HTTP listening on %s", *httpAddr)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("http serve stopped: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down registry")
	grpcServer.GracefulStop()

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(shutdownCtx)
}
