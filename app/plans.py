from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    daily_limit: int  # songs (plays) per day
    price_inr: int  # rupees per plan period (0 = free)

    @property
    def price_paise(self) -> int:
        return self.price_inr * 100


FREE = Plan("free", "Free", 1000, 0)

PLANS: dict[str, Plan] = {
    "free": FREE,
    "p2000": Plan("p2000", "2000 songs/day", 2000, 29),
    "p3000": Plan("p3000", "3000 songs/day", 3000, 39),
    "p4000": Plan("p4000", "4000 songs/day", 4000, 44),
    # "5000+ till 10000 songs" tier: the cap is the top of that range.
    "p10000": Plan("p10000", "5000-10000 songs/day", 10000, 79),
}


def paid_plans() -> list[Plan]:
    return [p for p in PLANS.values() if p.price_inr > 0]
