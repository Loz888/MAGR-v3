package orders

import "testing"

func TestPaidRevenueCents(t *testing.T) {
 orders := []Order{{"a", "paid", 40}, {"a", "paid", 60}}
 if got := PaidRevenueCents(orders); got != 60 { t.Fatalf("got %d; want 60", got) }
}
