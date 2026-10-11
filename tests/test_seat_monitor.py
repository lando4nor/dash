import unittest

import seat_monitor as m

SVG = """<svg xmlns="http://www.w3.org/2000/svg">
<g id="zal">
  <g id="zal:row-1">
    <g><circle cx="0" cy="0" r="10"/><text>1a</text></g>
    <g><circle cx="30" cy="0" r="10"/><text>1</text></g>
    <g><circle cx="60" cy="0" r="10"/><text>2</text></g>
    <g><circle cx="150" cy="0" r="10"/><text>3</text></g>
    <g><circle cx="180" cy="0" r="10"/><text>4</text></g>
    <g><circle cx="210" cy="0" r="10"/><text>5</text></g>
  </g>
  <g id="balcony:row-1"><g><circle cx="5" cy="9" r="10"/><text>1</text></g></g>
</g>
</svg>"""


class ParseTest(unittest.TestCase):
    def test_keys_match_ticket_api(self):
        rows = m.parse_hall_svg(SVG)
        self.assertEqual({("zal", 1), ("balcony", 1)}, set(rows))
        self.assertEqual("zal:row-1:seat-1a", rows[("zal", 1)][0].key)

    def test_central_zone_uses_geometry_across_aisle(self):
        row = m.parse_hall_svg(SVG)[("zal", 1)]
        # middle is x=105 (aisle between 2 and 3), so 2 and 3 are the closest pair
        self.assertEqual(["2", "3"], [s.label for s in m.central_zone(row, 2)])
        self.assertEqual(["1", "2", "3", "4"], [s.label for s in m.central_zone(row, 4)])

    def test_pick_sector(self):
        rows = m.parse_hall_svg(SVG)
        self.assertEqual("zal", m.pick_sector(rows, None))
        self.assertEqual("balcony", m.pick_sector(rows, "balcony"))

    def test_price_falls_back_to_color_price(self):
        self.assertEqual("1790", m.ticket_price({"price": None, "colorPrice": {"price": "1790.00"}}))
        self.assertEqual("990", m.ticket_price({"price": 0.0, "colorPrice": {"price": "990.00"}}))


if __name__ == "__main__":
    unittest.main()
