FROM golang:1.26 AS builder
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/lb-proxy ./cmd/proxy
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/lb-edge ./cmd/edge
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/lb-k8s-agent ./cmd/k8s-agent

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /out/lb-proxy /usr/local/bin/lb-proxy
COPY --from=builder /out/lb-edge /usr/local/bin/lb-edge
COPY --from=builder /out/lb-k8s-agent /usr/local/bin/lb-k8s-agent
ENTRYPOINT ["/usr/local/bin/lb-edge"]
