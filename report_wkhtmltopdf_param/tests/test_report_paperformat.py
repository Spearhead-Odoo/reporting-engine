# Copyright 2017 Avoin.Systems
# Copyright 2017 Eficent Business and IT Consulting Services, S.L.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging

import odoo.tests
from odoo.exceptions import ValidationError
from odoo.tests.common import tagged


@tagged("post_install", "-at_install")
class TestWkhtmltopdf(odoo.tests.TransactionCase):
    def test_wkhtmltopdf_incorrect_parameter(self):
        for report_paperformat in self.env["report.paperformat"].search([]):
            with (
                self.assertRaises(ValidationError),
                self.assertLogs(level=logging.WARNING),
            ):
                report_paperformat.update(
                    {"custom_params": [(0, 0, {"name": "bad-parameter"})]}
                )

    def test_wkhtmltopdf_valid_parameter(self):
        for report_paperformat in self.env["report.paperformat"].search([]):
            error = False
            try:
                report_paperformat.update(
                    {"custom_params": [(0, 0, {"name": "--disable-smart-shrinking"})]}
                )
            except ValidationError:
                error = True
            self.assertEqual(
                error,
                False,
                "There was an error adding wkhtmltopdf "
                "parameter --disable-smart-shrinking",
            )
