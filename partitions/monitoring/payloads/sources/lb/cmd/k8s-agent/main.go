package main

import (
	"context"
	"flag"
	"log"
	"os/signal"
	"syscall"

	"github.com/rydzu/ainfra/lb/pkg/k8sagent"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func main() {
	var (
		registryAddr = flag.String("registry-addr", "127.0.0.1:50051", "registry gRPC address")
		namespace    = flag.String("namespace", metav1.NamespaceAll, "namespace to watch")
	)
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	agent, err := k8sagent.New(ctx, *registryAddr)
	if err != nil {
		log.Fatalf("create k8s agent: %v", err)
	}

	if err := agent.Run(ctx, *namespace); err != nil {
		log.Fatalf("k8s agent exited: %v", err)
	}
}
