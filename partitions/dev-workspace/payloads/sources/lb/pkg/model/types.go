package model

type Backend struct {
	TargetIP   string `json:"target_ip"`
	TargetPort int32  `json:"target_port"`
}

type ServiceRoute struct {
	Account      string    `json:"account"`
	ServiceName  string    `json:"service_name"`
	Protocol     string    `json:"protocol,omitempty"`
	Description  string    `json:"description,omitempty"`
	ExternalPort int32     `json:"external_port"`
	Backends     []Backend `json:"backends"`
}

type Snapshot struct {
	Services []ServiceRoute `json:"services"`
}
