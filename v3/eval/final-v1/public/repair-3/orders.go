package orders

type Order struct {
    ID string
    Status string
    Amount int
}

func PaidRevenueCents(orders []Order) int {
    total := 0
    for _, order := range orders {
        total += order.Amount
    }
    return total
}
