import base64
import copy
import json
import logging
import uuid
import zlib

from jinja2 import Template
from jinja2.exceptions import TemplateSyntaxError

from ops.charm import (
    CharmBase,
    CharmEvents,
    RelationBrokenEvent,
    RelationChangedEvent,
)
from ops.model import Relation, Unit
from ops.framework import EventBase, EventSource, StoredState
from ops.relation import ConsumerBase, ProviderBase

from typing import Dict, List, Union

LIBID = "987654321"
LIBAPI = 1
LIBPATCH = 0

logger = logging.getLogger(__name__)


class GrafanaDashboardsChanged(EventBase):
    """Event emitted when Grafana dashboards change"""

    def __init__(self, handle, data=None):
        super().__init__(handle)
        self.data = data

    def snapshot(self) -> Dict:
        """Save grafana source information"""
        return {"data": self.data}

    def restore(self, snapshot):
        """Restore grafana source information"""
        self.data = snapshot["data"]


class GrafanaDashboardEvents(CharmEvents):
    """Events raised by :class:`GrafanaSourceEvents`"""

    dashboards_changed = EventSource(GrafanaDashboardsChanged)


class GrafanaDashboardEvent(EventBase):
    """Event emitted when Grafana dashboards cannot be resolved so we can
    set a status on the consumer
    """

    def __init__(self, handle, error_message: str = "", valid: bool = False):
        super().__init__(handle)
        self.error_message = error_message
        self.valid = valid

    def snapshot(self) -> Dict:
        """Save grafana source information"""
        return {"error_message": self.error_message, "valid": self.valid}

    def restore(self, snapshot):
        """Restore grafana source information"""
        self.error_message = snapshot["error_message"]
        self.valid = snapshot["valid"]


class GrafanaDashboardConsumer(ConsumerBase):
    _stored = StoredState()

    def __init__(
        self,
        charm: CharmBase,
        name: str,
        consumes: dict,
        multi: bool = False,
    ) -> None:
        """Construct a Grafana dashboard charm client.

        The :class:`GrafanaDashboardConsumer` object provides an interface
        to Grafana. This interface supports providing additional
        dashboards for Grafana to display. For example, if a charm
        exposes some metrics which are consumable by a dashboard
        (such as Prometheus), then an additional dashboard can be added
        by instantiating a :class:`GrafanaDashboardConsumer` object and
        adding its datasources as follows:

            self.grafana = GrafanaConsumer(self, "grafana-source", {"grafana-source"}: ">=2.0"})
            self.grafana.add_dashboard(data: str)

        Args:

            charm: a :class:`CharmBase` object which manages this
                :class:`GrafanaConsumer` object. Generally this is
                `self` in the instantiating class.
            name: a :string: name of the relation between `charm`
                the Grafana charmed service.
            consumes: a :dict: of acceptable monitoring service
                providers. The keys of the dictionary are :string:
                names of grafana source service providers. Typically,
                this is `grafana-source`. The values of the dictionary
                are corresponding minimal acceptable semantic versions
                for the service.
            multi: an optional (default `False`) flag to indicate if
                this object should support interacting with multiple
                service providers.

        """
        super().__init__(charm, name, consumes, multi)

        self.charm = charm
        self._stored.set_default(dashboards={})

        events = self.charm.on[name]
        self.on.define_event("dashboard_status_changed", GrafanaDashboardEvent)
        self.framework.observe(
            events.relation_changed, self._on_grafana_dashboard_relation_changed
        )

    def add_dashboard(self, data: str, rel_id=None) -> None:
        """
        Add a dashboard to Grafana. `data` should be a string representing
        a jinja template which can be templated with the appropriate
        `grafana_datasource` and `prometheus_job_name`
        """
        rel = self.framework.model.get_relation(self.name, rel_id)
        rel_id = rel_id if rel_id is not None else rel.id

        prom_rel = self.framework.model.get_relation("monitoring")

        try:
            prom_unit = prom_rel.units.pop()
        except (IndexError, AttributeError):
            error_message = (
                "Waiting for a prometheus_scrape relation to send dashboard data"
            )
            self.on.dashboard_status_changed.emit(
                error_message=error_message, valid=False
            )
            return

        self._update_dashboards(data, rel_id, prom_unit)

    def _on_grafana_dashboard_relation_changed(
        self, event: RelationChangedEvent
    ) -> None:
        """
        Watch for changes so we know if there's an error to signal back to the
        parent charm
        """
        if not self.charm.unit.is_leader():
            return

        data = json.loads(event.relation.data[event.app].get("event", "{}"))

        if not data:
            return

        error_message = data.get("errors", "")
        if error_message:
            self.on.dashboard_status_changed.emit(
                error_message=data.get("errors", ""), valid=data.get("valid", False)
            )
            return

        valid_message = data.get("valid", False)
        if valid_message:
            self.on.dashboard_status_changed.emit(valid=True)

    def _update_dashboards(self, data: str, rel_id: int, prom_unit: Unit) -> None:
        """
        Update the dashboards in the relation data bucket
        """
        if not self.charm.unit.is_leader():
            return

        prom_identifier = "{}_{}_{}".format(
            prom_unit._backend.model_name,
            prom_unit._backend.model_uuid,
            prom_unit.app.name,
        )

        prom_target = "{} [ {} / {} ]".format(
            self.charm.app.name.capitalize(),
            self.charm.model.name,
            self.charm.model.uuid,
        )

        prom_query = (
            "juju_model='{}',juju_model_uuid='{}',juju_application='{}'".format(
                self.charm.model.name, self.charm.model.uuid, self.charm.app.name
            )
        )

        # It's completely ridiculous to add a UUID, but if we don't have some
        # pseudo-random value, this never makes it across 'juju set-state'
        stored_data = {
            "monitoring_identifier": prom_identifier,
            "monitoring_target": prom_target,
            "monitoring_query": prom_query,
            "template": base64.b64encode(zlib.compress(data.encode(), 9)).decode(),
            "removed": False,
            "invalidated": False,
            "invalidated_reason": "",
            "uuid": str(uuid.uuid4()),
        }
        rel = self.framework.model.get_relation(self.name, rel_id)

        self._stored.dashboards[rel_id] = stored_data
        rel.data[self.charm.app]["dashboards"] = json.dumps(stored_data)

    def remove_dashboard(self, rel_id=None) -> None:
        if not self.charm.unit.is_leader():
            return

        if rel_id is None:
            rel_id = self.relation_id

        rel = self.framework.model.get_relation(self.name, rel_id)

        dash = self._stored.dashboards[rel.id].pop()
        dash["removed"] = True

        rel.data[self.charm.app]["dashboards"] = json.dumps(dash)

    def invalidate_dashboard(self, reason: str, rel_id=None) -> None:
        if not self.charm.unit.is_leader():
            return

        if rel_id is None:
            rel_id = self.relation_id

        rel = self.framework.model.get_relation(self.name, rel_id)

        dash = self._stored.dashboards[rel.id]
        dash["invalidated"] = True
        dash["invalidated_reason"] = reason

        rel.data[self.charm.app]["dashboards"] = json.dumps(dict(dash))

    @property
    def dashboards(self) -> List:
        return [v for v in self._stored.dashboards.values()]


class GrafanaDashboardProvider(ProviderBase):
    on = GrafanaDashboardEvents()
    _stored = StoredState()

    def __init__(self, charm: CharmBase, name: str, service: str, version=None) -> None:
        """A Grafana based Monitoring service consumer

        Args:
            charm: a :class:`CharmBase` instance that manages this
                instance of the Grafana dashboard service.
            name: string name of the relation that is provides the
                Grafana dashboard service.
            service: string name of service provided. This is used by
                :class:`GrafanaDashboardProvider` to validate this service as
                acceptable. Hence the string name must match one of the
                acceptable service names in the :class:`GrafanaDashboardProvider`s
                `consumes` argument. Typically this string is just "grafana".
            version: a string providing the semantic version of the Grafana
                dashboard being provided.

        """
        super().__init__(charm, name, service, version)

        self.charm = charm
        events = self.charm.on[name]

        self._stored.set_default(
            dashboards=dict(),
            invalid_dashboards=dict(),
            active_sources=[],
        )

        self.framework.observe(
            events.relation_changed, self._on_grafana_dashboard_relation_changed
        )
        self.framework.observe(
            events.relation_broken, self._on_grafana_dashboard_relation_broken
        )

    def _on_grafana_dashboard_relation_changed(
        self, event: RelationChangedEvent
    ) -> None:
        """Handle relation changes in related consumers.

        If there are changes in relations between Grafana dashboard providers
        and consumers, this event handler (if the unit is the leader) will
        get data for an incoming grafana-dashboard relation through a
        :class:`GrafanaDashboardssChanged` event, and make the relation data
        is available in the app's datastore object. The Grafana charm can
        then respond to the event to update its configuration
        """
        if not self.charm.unit.is_leader():
            return

        rel = event.relation

        data = (
            json.loads(event.relation.data[event.app].get("dashboards", {}))
            if event.relation.data[event.app].get("dashboards", {})
            else None
        )
        if not data:
            logger.info("No dashboard data found in relation")
            return

        # Get rid of this now that we passed through to the other side
        data.pop("uuid", None)

        # Pop it out of the list of dashboards if a relation is broken externally
        if data.get("removed", False):
            self._stored.dashboards.pop(rel.id)
            return

        if data.get("invalidated", False):
            self._stored.invalid_dashboards[rel.id] = data
            self._purge_dead_dashboard(rel.id)
            rel.data[self.charm.app]["event"] = json.dumps(
                {"errors": data.get("invalidated_reason"), "valid": False}
            )
            return

        if not self._stored.active_sources:
            msg = "Cannot add Grafana dashboard. No configured datasources"
            self._stored.invalid_dashboards[rel.id] = data
            self._purge_dead_dashboard(rel.id)
            logger.warning(msg)
            rel.data[self.charm.app]["event"] = json.dumps(
                {"errors": msg, "valid": False}
            )
            return

        self._validate_dashboard_data(data, rel)

    def _validate_dashboard_data(self, data: Dict, rel: Relation) -> None:
        """
        Verify that the passed dashboard data is able to be found in our list
        of datasources and will render. If they do, let the charm know by
        emitting an event.
        """
        grafana_datasource = self._find_grafana_datasource(data, rel)
        if not grafana_datasource:
            return

        # The dashboards are WAY too big since this ultimately calls out to Juju to set the relation data,
        # and it overflows the maximum argument length for subprocess, so we have to use b64, annoyingly.

        # Worse, Python3 expects absolutely everything to be a byte, and a plain `base64.b64encode()` is still
        # too large, so we have to go through hoops of encoding to byte, compressing with zlib, converting
        # to base64 so it can be converted to JSON, then all the way back
        try:
            tm = Template(
                zlib.decompress(base64.b64decode(data["template"].encode())).decode()
            )
        except TemplateSyntaxError:
            self._purge_dead_dashboard(rel.id)
            msg = "Cannot add Grafana dashboard. Template is not valid Jinja"
            logger.warning(msg)
            rel.data[self.charm.app]["event"] = json.dumps(
                {"errors": msg, "valid": False}
            )
            return

        msg = tm.render(
            grafana_datasource=grafana_datasource,
            prometheus_target=data["monitoring_target"],
            prometheus_query=data["monitoring_query"],
        )

        msg = {
            "target": data["monitoring_identifier"],
            "dashboard": base64.b64encode(zlib.compress(msg.encode(), 9)).decode(),
            "data": data,
        }

        # Remove it from the list of invalid dashboards if it's there, and
        # send data back to the providing charm so it knows this dashboard is
        # valid now
        if self._stored.invalid_dashboards.pop(rel.id, None):
            rel.data[self.charm.app]["event"] = json.dumps(
                {"errors": "", "valid": True}
            )

        stored_data = self._stored.dashboards.get(rel.id, {}).get("data", {})
        coerced_data = dict(stored_data) if stored_data else {}

        if not coerced_data == msg["data"]:
            self._stored.dashboards[rel.id] = msg
            self.on.dashboards_changed.emit()

    def _find_grafana_datasource(self, data: Dict, rel: Relation) -> Union[str, None]:
        """
        Loop through the provider data and try to find a matching datasource. Return it
        if possible, otherwise add it to the list of invalid dashboards.

        May return either a :str: if a datasource is found, or :None: if it cannot be
        resolved
        """
        try:
            grafana_datasource = "{}".format(
                [
                    x["source-name"]
                    for x in self._stored.active_sources
                    if data["monitoring_identifier"] in x["source-name"]
                ][0]
            )
        except IndexError:
            msg = "Cannot find a Grafana datasource matching the dashboard"
            self._stored.invalid_dashboards[rel.id] = data
            self._purge_dead_dashboard(rel.id)
            logger.warning(msg)
            rel.data[self.charm.app]["event"] = json.dumps(
                {"errors": msg, "valid": False}
            )
            return
        return grafana_datasource

    def _check_active_data_sources(self, data: Dict, rel: Relation) -> bool:
        """
        A trivial check to see whether there are any active datasources or not, used
        by both new dashboard additions and trying to restore invalid ones. Returns
        a :bool:
        """
        if not self._stored.active_sources:
            msg = "Cannot add Grafana dashboard. No configured datasources"
            self._stored.invalid_dashboards[rel.id] = data
            self._purge_dead_dashboard(rel.id)
            logger.warning(msg)
            rel.data[self.charm.app]["event"] = json.dumps(
                {"errors": msg, "valid": False}
            )

            return False
        return True

    def renew_dashboards(self, sources: List) -> None:
        """
        If something changes between this library and a datasource, try to re-establish
        invalid dashboards and invalidate active ones
        """
        # Cannot nest StoredDict inside StoredList
        self._stored.active_sources = [dict(s) for s in sources]

        # Make copies so we don't mutate these during iteration
        invalid_dashboards = copy.deepcopy(dict(self._stored.invalid_dashboards))
        active_dashboards = copy.deepcopy(dict(self._stored.dashboards))

        for rel_id, data in invalid_dashboards.items():
            rel = self.framework.model.get_relation(self.name, rel_id)
            self._validate_dashboard_data(dict(data), rel)

        # Check the active dashboards also in case a source was removed
        for rel_id, stored in active_dashboards.items():
            rel = self.framework.model.get_relation(self.name, rel_id)
            self._validate_dashboard_data(dict(stored["data"]), rel)

    def _on_grafana_dashboard_relation_broken(self, event: RelationBrokenEvent) -> None:
        """Update job config when consumers depart.

        When a Grafana dashboard consumer departs, the configuration
        for that consumer is removed from the list of dashboards
        """
        if not self.charm.unit.is_leader():
            return

        rel_id = event.relation.id
        try:
            self._stored.dashboards.pop(rel_id, None)
            self.on.dashboards_changed.emit()
        except KeyError:
            logger.warning("Could not remove dashboard for relation: {}".format(rel_id))

    def _purge_dead_dashboard(self, rel_id: int) -> None:
        """If an errored dashboard is in stored data, remove it and trigger a deletion"""
        if self._stored.dashboards.pop(rel_id, None):
            self.on.dashboards_changed.emit()

    @property
    def dashboards(self) -> List:
        """
        Returns a list of known dashboards
        """
        dashboards = []
        for dash in self._stored.dashboards.values():
            dashboards.append(dash)
        return dashboards
