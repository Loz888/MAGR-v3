package orders

import "testing"

func TestPaidRevenueCents(t *testing.T) {
 orders := []Order{{"a", "paid", 70}, {"a", "paid", 60}}
 if got := PaidRevenueCents(orders); got != 100 { t.Fatalf("got %d; want 100", got) }
}
