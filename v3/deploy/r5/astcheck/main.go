// Parse only: candidate code is data, never executed by this host helper.
package main

import (
	"bytes"
	"encoding/json"
	"go/ast"
	"go/format"
	"go/parser"
	"go/token"
	"io"
	"os"
	"strings"
)

type Input struct {
	Before string
	After  string
}

func main() {
	b, _ := io.ReadAll(io.LimitReader(os.Stdin, 131073))
	var in Input
	if len(b) > 131072 || json.Unmarshal(b, &in) != nil {
		os.Exit(1)
	}
	old, ok := inspect(in.Before, false)
	if !ok {
		os.Exit(1)
	}
	now, ok := inspect(in.After, true)
	if !ok || old != now {
		os.Exit(1)
	}
	os.Stdout.Write([]byte("{\"passed\":true}\n"))
}
func inspect(source string, check bool) (string, bool) {
	for _, v := range []string{"//go:", "//line", "/*go:"} {
		if strings.Contains(source, v) {
			return "", false
		}
	}
	fs := token.NewFileSet()
	f, e := parser.ParseFile(fs, "orders.go", source, 0)
	if e != nil || len(f.Imports) != 0 {
		return "", false
	}
	found := false
	valid := true
	for _, d := range f.Decls {
		fn, ok := d.(*ast.FuncDecl)
		if !ok || fn.Name.Name != "PaidRevenueCents" {
			continue
		}
		if found || fn.Recv != nil {
			return "", false
		}
		found = true
		if check {
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				switch x := n.(type) {
				case *ast.CallExpr:
					id, ok := x.Fun.(*ast.Ident)
					if !ok || (id.Name != "make" && id.Name != "len") {
						valid = false
					}
				case *ast.SelectorExpr:
					if x.Sel.Name != "ID" && x.Sel.Name != "Status" && x.Sel.Name != "Amount" {
						valid = false
					}
				case *ast.GoStmt, *ast.DeferStmt, *ast.FuncLit, *ast.ChanType, *ast.SendStmt:
					valid = false
				}
				return true
			})
		}
		fn.Body = nil
	}
	var out bytes.Buffer
	e = format.Node(&out, fs, f)
	return out.String(), found && valid && e == nil
}
