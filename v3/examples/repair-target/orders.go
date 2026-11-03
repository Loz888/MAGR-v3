package orders

type Order struct {
    ID string
    Status string
    Amount int
}

func PaidRevenueCents(orders []Order) int {
    seen := make(map[string]bool)
    total := 0
    for _, order := range orders {
        if seen[order.ID] {
            continue
        }
        if order.Status == "paid" {
            seen[order.ID] = true
            total += order.Amount
        }
    }
    return total
}
