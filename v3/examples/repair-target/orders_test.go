package orders

import "testing"

func TestPaidRevenueCents(t *testing.T) {
    input := []Order{{"a", "paid", 37}, {"a", "paid", 37}, {"b", "cancelled", 99}, {"c", "paid", 13}}
    if got := PaidRevenueCents(input); got != 50 { t.Fatalf("paid unique revenue: got=%d want=50", got) }
}
