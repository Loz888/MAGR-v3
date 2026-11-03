package orders

import "testing"

func TestPaidRevenueCents(t *testing.T) {
 orders := []Order{{"a", "paid", -5}, {"a", "paid", 40}}
 if got := PaidRevenueCents(orders); got != 40 { t.Fatalf("got %d; want 40", got) }
}
