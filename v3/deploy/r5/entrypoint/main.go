// Trusted stdin-only launcher. Source code never executes on the host.
package main

import (
	"archive/tar"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "prepare" {
		cmd := exec.Command("/usr/local/go/bin/go", "build", "testing", "net", "encoding/json", "os/exec")
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		if cmd.Run() != nil {
			os.Exit(125)
		}
		err := filepath.WalkDir("/opt", func(path string, d fs.DirEntry, e error) error {
			if e != nil {
				return e
			}
			if d.IsDir() {
				return os.Chmod(path, 0755)
			}
			return os.Chmod(path, 0644)
		})
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(125)
		}
		return
	}
	os.Exit(run())
}
func run() int {
	dir, err := os.MkdirTemp("/tmp", "r5-")
	if err != nil {
		fmt.Fprintln(os.Stderr, "workspace:", err)
		return 125
	}
	if err = copyCache("/opt/gocache", "/tmp/go-cache"); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 125
	}
	version, err := exec.Command("/usr/local/go/bin/go", "version").CombinedOutput()
	if err != nil {
		fmt.Fprintln(os.Stderr, "toolchain:", err, string(version))
		return 125
	}
	header, _ := json.Marshal(map[string]string{"sandbox_toolchain": strings.TrimSpace(string(version))})
	fmt.Println(string(header))
	reader := tar.NewReader(io.LimitReader(os.Stdin, 2*1024*1024+1))
	count := 0
	total := int64(0)
	for {
		h, e := reader.Next()
		if e == io.EOF {
			break
		}
		if e != nil {
			return 125
		}
		count++
		total += h.Size
		if count > 32 || total > 1024*1024 || h.Size < 0 || h.Size > 131072 || h.Typeflag != tar.TypeReg || !fs.ValidPath(h.Name) || strings.Contains(h.Name, "\\") {
			return 125
		}
		target := filepath.Join(dir, h.Name)
		if e = os.MkdirAll(filepath.Dir(target), 0700); e != nil {
			return 125
		}
		f, e := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if e != nil {
			return 125
		}
		_, e = io.CopyN(f, reader, h.Size)
		c := f.Close()
		if e != nil || c != nil {
			return 125
		}
	}
	if count == 0 {
		return 125
	}
	command := exec.Command("/usr/local/go/bin/go", "test", "-count=1", "-json", "-vet=off", "-buildvcs=false", "./...")
	command.Dir = dir
	command.Env = []string{"PATH=/usr/local/go/bin", "HOME=/tmp", "GOCACHE=/tmp/go-cache", "GOTOOLCHAIN=local", "GOPROXY=off", "GOSUMDB=off", "GOVCS=*:off", "GOWORK=off", "GOENV=off", "CGO_ENABLED=0", "GOMAXPROCS=2", "TMPDIR=/tmp"}
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	if e := command.Run(); e != nil {
		if exit, ok := e.(*exec.ExitError); ok {
			return exit.ExitCode()
		}
		return 125
	}
	return 0
}
func copyCache(source, target string) error {
	return filepath.WalkDir(source, func(path string, d fs.DirEntry, e error) error {
		if e != nil {
			return e
		}
		rel, e := filepath.Rel(source, path)
		if e != nil {
			return e
		}
		out := filepath.Join(target, rel)
		if d.IsDir() {
			return os.MkdirAll(out, 0700)
		}
		if !d.Type().IsRegular() {
			return fmt.Errorf("invalid image cache")
		}
		b, e := os.ReadFile(path)
		if e != nil {
			return e
		}
		return os.WriteFile(out, b, 0600)
	})
}
