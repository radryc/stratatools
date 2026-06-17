package registry

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sort"
	"strings"

	"github.com/rydzu/ainfra/lb/pkg/pb"
)

type GRPCServer struct {
	pb.UnimplementedDiscoveryRegistryServer
	state *State
}

func NewGRPCServer(state *State) *GRPCServer {
	return &GRPCServer{state: state}
}

func (s *GRPCServer) RegisterService(ctx context.Context, req *pb.RegisterServiceRequest) (*pb.RegisterServiceResponse, error) {
	externalPort, err := s.state.Register(req)
	if err != nil {
		return nil, err
	}
	return &pb.RegisterServiceResponse{ExternalPort: externalPort}, nil
}

func (s *GRPCServer) DeregisterService(ctx context.Context, req *pb.DeregisterServiceRequest) (*pb.DeregisterServiceResponse, error) {
	removed, err := s.state.Deregister(req)
	if err != nil {
		return nil, err
	}
	return &pb.DeregisterServiceResponse{Removed: removed}, nil
}

func (s *GRPCServer) ListServices(ctx context.Context, req *pb.ListServicesRequest) (*pb.ListServicesResponse, error) {
	return s.state.ListResponse(), nil
}

func HTTPServicesHandler(state *State) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/services", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		if err := json.NewEncoder(w).Encode(state.Snapshot()); err != nil {
			log.Printf("encode /services response: %v", err)
		}
	})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" && r.URL.Path != "/ui" {
			http.NotFound(w, r)
			return
		}
		snap := state.Snapshot()

		// Group services by account, sort for stable output.
		byAccount := map[string][]serviceUI{}
		for _, svc := range snap.Services {
			acc := svc.Account
			if acc == "" {
				acc = "default"
			}
			proto := strings.ToLower(svc.Protocol)
			scheme := "http"
			if proto == "grpc" {
				scheme = "grpc"
			}
			// Build the host from the request so the link works from any machine.
			host := r.Host
			if h, _, err := splitHostPort(host); err == nil {
				host = fmt.Sprintf("%s:%d", h, svc.ExternalPort)
			} else {
				host = fmt.Sprintf("%s:%d", host, svc.ExternalPort)
			}
			url := fmt.Sprintf("%s://%s", scheme, host)
			byAccount[acc] = append(byAccount[acc], serviceUI{
				Name:        svc.ServiceName,
				Port:        svc.ExternalPort,
				Protocol:    svc.Protocol,
				Description: svc.Description,
				URL:         url,
			})
		}
		accounts := make([]string, 0, len(byAccount))
		for a := range byAccount {
			accounts = append(accounts, a)
		}
		sort.Strings(accounts)
		groups := make([]uiGroup, 0, len(accounts))
		for _, a := range accounts {
			svcs := byAccount[a]
			sort.Slice(svcs, func(i, j int) bool { return svcs[i].Port < svcs[j].Port })
			groups = append(groups, uiGroup{Account: a, Services: svcs})
		}

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := renderUI(w, groups); err != nil {
			log.Printf("render ui: %v", err)
		}
	})
	return mux
}

type serviceUI struct {
	Name        string
	Port        int32
	Protocol    string
	Description string
	URL         string
}

func splitHostPort(hostport string) (host string, port string, err error) {
	// net.SplitHostPort wrapper used to avoid importing net in this file.
	i := strings.LastIndex(hostport, ":")
	if i < 0 {
		return hostport, "", fmt.Errorf("no port")
	}
	return hostport[:i], hostport[i+1:], nil
}

type uiGroup struct {
	Account  string
	Services []serviceUI
}

func renderUI(w http.ResponseWriter, groups []uiGroup) error {
	_, err := fmt.Fprint(w, `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LB Edge — Services</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh;padding:2rem}
  h1{font-size:1.5rem;font-weight:700;color:#f8fafc;margin-bottom:1.5rem;display:flex;align-items:center;gap:.6rem}
  h1 span.dot{width:10px;height:10px;border-radius:50%;background:#22c55e;display:inline-block;box-shadow:0 0 6px #22c55e}
  h2{font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin:1.5rem 0 .75rem}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:.75rem}
  a.card{display:flex;flex-direction:column;gap:.35rem;background:#1e2330;border:1px solid #2d3347;border-radius:10px;padding:1rem 1.1rem;text-decoration:none;color:inherit;transition:border-color .15s,background .15s}
  a.card:hover{background:#252c3d;border-color:#4f6af5}
  .card-top{display:flex;align-items:center;justify-content:space-between}
  .card-name{font-size:.95rem;font-weight:600;color:#f1f5f9}
  .badge{font-size:.65rem;font-weight:600;padding:.15rem .45rem;border-radius:4px;text-transform:uppercase;letter-spacing:.04em}
  .badge-http{background:#1e3a5f;color:#60a5fa}
  .badge-grpc{background:#2e1d5e;color:#a78bfa}
  .badge-tcp{background:#1a3a2a;color:#4ade80}
  .badge-{background:#2a2a2a;color:#94a3b8}
  .port{font-size:.8rem;color:#94a3b8;font-family:"SF Mono",Consolas,monospace}
  .desc{font-size:.78rem;color:#64748b;margin-top:.1rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .empty{color:#475569;font-size:.875rem;padding:1rem 0}
  @media(max-width:480px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1><span class="dot"></span> LB Edge &mdash; Services</h1>
`)
	if err != nil {
		return err
	}

	if len(groups) == 0 {
		_, err = fmt.Fprint(w, `<p class="empty">No services registered yet.</p>`)
		return err
	}

	for _, g := range groups {
		_, err = fmt.Fprintf(w, "<h2>%s</h2>\n<div class=\"grid\">\n", htmlEscape(g.Account))
		if err != nil {
			return err
		}
		for _, s := range g.Services {
			proto := strings.ToLower(s.Protocol)
			if proto == "" {
				proto = "tcp"
			}
			desc := s.Description
			if desc == "" {
				desc = s.Name
			}
			target := s.URL
			if proto == "grpc" {
				target = "#" // grpc can't be opened in browser directly
			}
			_, err = fmt.Fprintf(w,
				"<a class=\"card\" href=\"%s\" target=\"_blank\" rel=\"noopener\">"+
					"<div class=\"card-top\">"+
					"<span class=\"card-name\">%s</span>"+
					"<span class=\"badge badge-%s\">%s</span>"+
					"</div>"+
					"<span class=\"port\">:%d</span>"+
					"<span class=\"desc\">%s</span>"+
					"</a>\n",
				htmlEscape(target),
				htmlEscape(s.Name),
				proto,
				htmlEscape(strings.ToUpper(proto)),
				s.Port,
				htmlEscape(desc),
			)
			if err != nil {
				return err
			}
		}
		_, err = fmt.Fprint(w, "</div>\n")
		if err != nil {
			return err
		}
	}
	_, err = fmt.Fprint(w, "</body></html>")
	return err
}

func htmlEscape(s string) string {
	s = strings.ReplaceAll(s, "&", "&amp;")
	s = strings.ReplaceAll(s, "<", "&lt;")
	s = strings.ReplaceAll(s, ">", "&gt;")
	s = strings.ReplaceAll(s, "\"", "&#34;")
	return s
}
