package orders

type Order struct {
    ID string
    Status string
    Amount int
}

func PaidRevenueCents(orders []Order) int {
total := 0
seen := make(map[string]bool)
for _, order := range orders {
    if order.Status == "paid" && !seen[order.ID] {
        seen[order.ID] = true
        total += order.Amount
    }
}
return total
}
