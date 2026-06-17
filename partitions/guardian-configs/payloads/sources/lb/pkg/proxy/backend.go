package proxy

import (
	"fmt"
	"io"
	"net"
	"strconv"
	"sync/atomic"
	"time"
)

type backendState int32

const (
	stateActive backendState = iota
	stateDraining
	stateUnhealthy
)

type backend struct {
	ip   string
	port int32

	state         atomic.Int32
	activeConns   atomic.Int64
	consecFailure atomic.Int32
}

func newBackend(ip string, port int32) *backend {
	b := &backend{ip: ip, port: port}
	b.state.Store(int32(stateActive))
	return b
}

func (b *backend) addr() string {
	return net.JoinHostPort(b.ip, strconv.Itoa(int(b.port)))
}

func (b *backend) markDraining() {
	for {
		current := backendState(b.state.Load())
		if current == stateUnhealthy || current == stateDraining {
			return
		}
		if b.state.CompareAndSwap(int32(current), int32(stateDraining)) {
			return
		}
	}
}

func (b *backend) markActive() {
	b.state.Store(int32(stateActive))
}

func (b *backend) markUnhealthy() bool {
	for {
		current := backendState(b.state.Load())
		if current == stateUnhealthy {
			return false
		}
		if b.state.CompareAndSwap(int32(current), int32(stateUnhealthy)) {
			return true
		}
	}
}

func (b *backend) stateValue() backendState {
	return backendState(b.state.Load())
}

func writeProxyProtocolV1(dst net.Conn, src net.Conn) error {
	clientTCP, ok := src.RemoteAddr().(*net.TCPAddr)
	if !ok {
		return fmt.Errorf("client address is not TCP")
	}
	proxyTCP, ok := src.LocalAddr().(*net.TCPAddr)
	if !ok {
		return fmt.Errorf("proxy address is not TCP")
	}

	family := "TCP4"
	if clientTCP.IP.To4() == nil || proxyTCP.IP.To4() == nil {
		family = "TCP6"
	}

	header := fmt.Sprintf("PROXY %s %s %s %d %d\r\n", family, clientTCP.IP.String(), proxyTCP.IP.String(), clientTCP.Port, proxyTCP.Port)
	_ = dst.SetWriteDeadline(time.Now().Add(2 * time.Second))
	_, err := io.WriteString(dst, header)
	_ = dst.SetWriteDeadline(time.Time{})
	return err
}
