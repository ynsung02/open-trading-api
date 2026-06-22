from unittest import TestCase

from trader import (
    krx_stock_tick_size,
    round_buy_price_to_tick,
    round_sell_price_to_tick,
)


class KrxPriceTickTests(TestCase):
    def test_tick_size_boundaries(self) -> None:
        cases = {
            1_999: 1,
            2_000: 5,
            4_999: 5,
            5_000: 10,
            19_999: 10,
            20_000: 50,
            49_999: 50,
            50_000: 100,
            199_999: 100,
            200_000: 500,
            499_999: 500,
            500_000: 1_000,
        }
        for price, expected in cases.items():
            with self.subTest(price=price):
                self.assertEqual(krx_stock_tick_size(price), expected)

    def test_sell_354750_rounds_up_to_355000(self) -> None:
        self.assertEqual(round_sell_price_to_tick(354_750), 355_000)

    def test_buy_350750_rounds_down_to_350500(self) -> None:
        self.assertEqual(round_buy_price_to_tick(350_750), 350_500)
