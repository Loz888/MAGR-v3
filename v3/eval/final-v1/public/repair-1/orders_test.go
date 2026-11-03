package orders

import "testing"

func TestPaidRevenueCents(t *testing.T) {
 orders := []Order{{"a", "paid", 40}, {"a", "cancelled", 99}}
 if got := PaidRevenueCents(orders); got != 0 { t.Fatalf("got %d; want 0", got) }
}
