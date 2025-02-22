# Copyright (C) 2015 Akretion (<http://www.akretion.com>)
# Copyright (C) 2017 - Today: GRAP (http://www.grap.coop)
# @author: Sylvain LE GAL (https://twitter.com/legalsylvain)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

import base64
import logging
import re
import uuid
from io import BytesIO

from psycopg2 import ProgrammingError
from psycopg2.sql import SQL

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError, ValidationError

from ..sql_db import get_external_cursor

logger = logging.getLogger(__name__)


class SQLRequestMixin(models.AbstractModel):
    _name = "sql.request.mixin"
    _inherit = ["mail.thread"]

    _description = "SQL Request Mixin"

    _clean_query_enabled = True

    _check_prohibited_words_enabled = True

    _check_execution_enabled = True

    _sql_request_groups_relation = False

    _sql_request_users_relation = False

    STATE_SELECTION = [("draft", "Draft"), ("sql_valid", "SQL Valid")]

    PROHIBITED_WORDS = [
        "delete",
        "drop",
        "insert",
        "alter",
        "truncate",
        "execute",
        "create",
        "update",
        "ir_config_parameter",
    ]

    # Default Section
    @api.model
    def _default_group_ids(self):
        ir_model_obj = self.env["ir.model.data"]
        return [
            ir_model_obj._xmlid_to_res_id("sql_request_abstract.group_sql_request_user")
        ]

    @api.model
    def _default_user_ids(self):
        return []

    # Columns Section
    name = fields.Char(required=True)

    note = fields.Html()

    query = fields.Text(
        required=True,
        help="You can't use the following words"
        ": DELETE, DROP, CREATE, INSERT, ALTER, TRUNCATE, EXECUTE, UPDATE.",
    )

    state = fields.Selection(
        selection=STATE_SELECTION,
        default="draft",
        help="State of the Request:\n"
        " * 'Draft': Not tested\n"
        " * 'SQL Valid': SQL Request has been checked and is valid",
    )

    group_ids = fields.Many2many(
        comodel_name="res.groups",
        string="Allowed Groups",
        relation=_sql_request_groups_relation,
        column1="sql_id",
        column2="group_id",
        default=_default_group_ids,
    )

    user_ids = fields.Many2many(
        comodel_name="res.users",
        string="Allowed Users",
        relation=_sql_request_users_relation,
        column1="sql_id",
        column2="user_id",
        default=_default_user_ids,
    )
    use_external_database = fields.Boolean(
        help=(
            "If filled, the query will be executed against an external "
            "database, configured in Odoo main configuration file. "
        )
    )

    @api.constrains("use_external_database")
    def check_external_config(self):
        external_db_records = self.filtered(lambda rec: rec.use_external_database)
        if external_db_records:
            external_db_name = tools.config.get("external_db_name")
            if not external_db_name:
                raise ValidationError(
                    _(
                        "You can't use an external database as there are no such "
                        "configuration about this. Please contact "
                        "your Odoo administrator to solve this issue."
                    )
                )

    has_group_changed = fields.Boolean(
        copy=False,
        help="Technical fields, used in modules"
        " that depends on this one to know"
        " if groups has changed, and that according"
        " access should be updated.",
    )

    @api.onchange("group_ids")
    def onchange_group_ids(self):
        if self.state not in ("draft", "sql_valid"):
            self.has_group_changed = True

    # Action Section
    def button_validate_sql_expression(self):
        for item in self:
            if item._clean_query_enabled:
                item._clean_query()
            if item._check_prohibited_words_enabled:
                item._check_prohibited_words()
            if item._check_execution_enabled:
                item._check_execution()
            item.state = "sql_valid"

    def button_set_draft(self):
        self.write(
            {
                "has_group_changed": False,
                "state": "draft",
            }
        )

    # API Section
    def _execute_sql_request(
        self,
        params=None,
        mode="fetchall",
        rollback=True,
        view_name=False,
        copy_options="CSV HEADER DELIMITER ';'",
        header=False,
    ):
        """Execute a SQL request on the current database.

        ??? This function checks before if the user has the
        right to execute the request.

        :param params: (dict) of keys / values that will be replaced in
            the sql query, before executing it.
        :param mode: (str) result type expected. Available settings :
            * 'view': create a view with the select query. Extra param
                required 'view_name'.
            * 'materialized_view': create a MATERIALIZED VIEW with the
                select query. Extra parameter required 'view_name'.
            * 'fetchall': execute the select request, and return the
                result of 'cr.fetchall()'.
            * 'fetchone' : execute the select request, and return the
                result of 'cr.fetchone()'
        :param rollback: (boolean) mention if a rollback should be played after
            the execution of the query. Please keep this feature enabled
            for security reason, except if necessary.
            (Ignored if @mode in ('view', 'materialized_view'))
        :param view_name: (str) name of the view.
            (Ignored if @mode not in ('view', 'materialized_view'))
        :param copy_options: (str) mentions extra options for
            "COPY request STDOUT WITH xxx" request.
            (Ignored if @mode != 'stdout')
        :param header: (boolean) if true, the header of the query will be
            returned as first element of the list if the mode is fetchall.
            (Ignored if @mode != fetchall)

        ..note:: The following exceptions could be raised:
            psycopg2.ProgrammingError: Error in the SQL Request.
            odoo.exceptions.UserError:
                * 'mode' is not implemented.
                * materialized view is not supported by the Postgresql Server.
        """
        self.ensure_one()
        res = False
        # Check if the request is in a valid state
        if self.state == "draft":
            raise UserError(_("It is not allowed to execute a not checked request."))

        # Disable rollback if a creation of a view is asked
        if mode in ("view", "materialized_view"):
            rollback = False

        query = self.env.cr.mogrify(self.query, params).decode("utf-8")

        if mode in ("fetchone", "fetchall"):
            pass
        elif mode == "stdout":
            query = SQL("COPY ({0}) TO STDOUT WITH {1}").format(
                SQL(query), SQL(copy_options)
            )
        elif mode in "view":
            query = SQL("CREATE VIEW {0} AS ({1});").format(SQL(query), SQL(view_name))
        elif mode in "materialized_view":
            self._check_materialized_view_available()
            query = SQL("CREATE MATERIALIZED VIEW {0} AS ({1});").format(
                SQL(query), SQL(view_name)
            )
        else:
            raise UserError(_("Unimplemented mode : '%s'") % mode)

        query_cr = self._get_cr_for_query()

        if rollback:
            rollback_name = self._create_savepoint(query_cr)
        try:
            if mode == "stdout":
                output = BytesIO()
                query_cr.copy_expert(query, output)
                res = base64.b64encode(output.getvalue())
                output.close()
            else:
                query_cr.execute(query)
                if mode == "fetchall":
                    res = query_cr.fetchall()
                    if header:
                        colnames = [desc[0] for desc in self.env.cr.description]
                        res.insert(0, colnames)
                elif mode == "fetchone":
                    res = query_cr.fetchone()
        finally:
            self._rollback_savepoint(rollback_name, query_cr)

        return res

    # Private Section
    def _get_cr_for_query(self):
        self.ensure_one()
        if self.use_external_database:
            return get_external_cursor()
        else:
            return self.env.cr

    @api.model
    def _create_savepoint(self, cr):
        rollback_name = "{}_{}".format(self._name.replace(".", "_"), uuid.uuid1().hex)
        # pylint: disable=sql-injection
        req = f"SAVEPOINT {rollback_name}"
        cr.execute(req)
        return rollback_name

    @api.model
    def _rollback_savepoint(self, rollback_name, cr):
        # pylint: disable=sql-injection
        req = f"ROLLBACK TO SAVEPOINT {rollback_name}"
        cr.execute(req)
        # close external database cursor
        if self.env.cr != cr:
            cr.close()

    @api.model
    def _check_materialized_view_available(self):
        self.env.cr.execute("SHOW server_version;")
        res = self.env.cr.fetchone()[0].split(".")
        minor_version = float(".".join(res[:2]))
        if minor_version < 9.3:
            raise UserError(
                _(
                    "Materialized View requires PostgreSQL 9.3 or greater but"
                    " PostgreSQL %s is currently installed."
                )
                % (minor_version)
            )

    def _clean_query(self):
        self.ensure_one()
        query = self.query.strip()
        while query[-1] == ";":
            query = query[:-1]
        self.query = query

    def _check_prohibited_words(self):
        """Check if the query contains prohibited words, to avoid maliscious
        SQL requests"""
        self.ensure_one()
        query = self.query.lower()
        for word in self.PROHIBITED_WORDS:
            expr = rf"\b{word}\b"
            is_not_safe = re.search(expr, query)
            if is_not_safe:
                raise UserError(
                    _(
                        "The query is not allowed because it contains unsafe word"
                        " '%s'"
                    )
                    % (word)
                )

    def _check_execution(self):
        """Ensure that the query is valid, trying to execute it. A rollback
        is done after."""
        self.ensure_one()
        query = self._prepare_request_check_execution()
        query_cr = self._get_cr_for_query()
        rollback_name = self._create_savepoint(query_cr)
        res = False
        try:
            query_cr.execute(query)
            res = self._hook_executed_request()
        except ProgrammingError as e:
            logger.exception("Failed query: %s", query)
            raise UserError(_("The SQL query is not valid:\n\n %s") % e) from e
        finally:
            self._rollback_savepoint(rollback_name, query_cr)
        return res

    def _prepare_request_check_execution(self):
        """Overload me to replace some part of the query, if it contains
        parameters"""
        self.ensure_one()
        return self.query

    def _hook_executed_request(self):
        """Overload me to insert custom code, when the SQL request has
        been executed, before the rollback.
        """
        self.ensure_one()
        return False

    def button_preview_sql_expression(self):
        self.button_validate_sql_expression()
        res = self._execute_sql_request()
        raise UserError("\n".join(map(lambda x: str(x), res[:100])))
