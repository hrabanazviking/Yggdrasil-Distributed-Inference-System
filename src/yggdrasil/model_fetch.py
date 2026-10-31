"""Model fetch duty — Y-037.

A node that lacks a model should not have to wait for an
operator to carry the weights to it by hand: the mesh carries
its own supplies. This slice is that duty — a node fetches a
model it lacks from a mesh peer or from a named source, when
policy allows, and the fetched bytes enter the node's model
store (Y-026) only after they prove themselves against the
pin the request itself carries.

- **The pin travels with the ask.** A fetch request
  (``model_fetch_request`` v1) names the model, its registry
  facts (family / quant / format / capabilities — declared,
  never evidence), the source (``peer`` + a peer node id, or
  ``named_source`` + the source's name), an opaque ``locator``
  the transport understands (a peer-side handle, a path on a
  named source — data for the transport, never interpreted,
  never executed, never a URL this module opens), and the
  SHA-256 + byte size the fetched file MUST measure. A request
  without a pin is not a fetch: there is nothing to verify
  against, and the schema refuses it before any byte moves.
- **Policy is the gate, and it is data.** A
  ``model_fetch_policy`` v1 says whether peer fetches are
  allowed at all, which named sources may be used (a source
  not named in the policy does not exist for the fetcher,
  however reachable it claims to be), the largest model this
  node will fetch (``max_model_bytes``, null = no cap), and
  the disk reserve that must survive the fetch
  (``min_free_disk_bytes`` — a node never fills its own disk
  to stock a model). Every gate is checked BEFORE the
  transport is asked for a single byte, and every refusal is
  a record naming its reason, never a silent no.
- **Bytes are verified before they are stocked.** The
  transport — an injected seam, exactly like the engine
  adapters' (Y-023–Y-025): a callable, or an object with
  ``fetch_chunks(request)``, yielding byte chunks — streams
  into a staging file under the store root's ``incoming/``
  directory while a SHA-256 is built chunk by chunk. A stream
  that runs past the expected size is cut off at the first
  chunk that crosses it. Only when the staged bytes measure
  exactly the pinned size and hash are they handed to the
  store's own ``add()`` — which copies, re-hashes, and pins
  them under the store's rules — and the store's verification
  is read back as the record's ``store_status``. Wrong bytes
  are deleted from staging and NEVER enter the store: a hash
  mismatch leaves the node exactly as stocked as before,
  plus a record that says so (``model_hash_mismatch``).
- **Presence short-circuits.** If the store already holds the
  name with the pinned hash, verified right now, the answer
  is ``already_present`` and the transport is never asked —
  the mesh does not carry supplies the node already has. If
  the name is registered with DIFFERENT bytes, the fetch is
  refused by name: fetching is not a way to re-point a name
  at other weights (that is Y-026's law, and it binds here).
  If the registered file is corrupt or missing, a fetch
  carrying the pin restores it — the staged bytes must still
  match the pin, and ``add()``'s idempotence keeps the
  original registration.
- **Every attempt is a record.** Each fetch — fetched,
  already present, refused, failed — lands as a validated
  ``model_fetch_record`` v1 (bytes actually received, the
  hash actually measured or null, the run time on an injected
  timer, the store's status afterwards) in the fetcher's
  ledger, is summarised by ``report()`` (a validated
  ``model_fetch_report`` v1), and is spoken as a Y-013
  EventEnvelope from the ``node_agent`` component. A source
  that cannot be reached fails as ``engine_unreachable``; a
  transport raising anything else fails as ``engine_error`` —
  named, recorded, and the staging file removed either way.

Honesty: there is NO network client in this module (the
Part I gate's banned-import scan stays green) and no real
peer or source has carried a model for this tree yet — the
suite's transports are in-process fakes speaking chunk
streams, and a fetcher built over one says so in every record
(``simulated: true``; the deployment wiring that builds a
fetcher over a real transport — the fabric's, Part III — is
what sets it false). Hash verification proves the bytes are
the pinned bytes; it does not prove the weights are good or
that the source was honest about anything else — the pin
itself has to come from a trusted ask (the registry, Y-008 /
Part IV), and that trust is the caller's, stated here, not
smuggled in.
"""

import hashlib
import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.model_store import ModelStore, ModelStoreError
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

# The defaults an operator starts from: peers may supply the
# mesh, but no named source exists until the operator names
# one, no size cap, and no disk reserve beyond the file itself.
# A node's own policy (deployment wiring) tightens these.
DEFAULT_POLICY = {
    "version": 1,
    "allow_peer_fetch": True,
    "named_sources": [],
    "max_model_bytes": None,
    "min_free_disk_bytes": 0,
}

_STAGING_DIR = "incoming"


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the registry pattern
    the earlier slices build."""
    schema = _load_schema(name)
    resources = []
    for path in sorted(_SCHEMAS.glob("*.json")):
        other = json.loads(path.read_text())
        resource = Resource.from_contents(other)
        resources.append((path.name, resource))
        resources.append((other["$id"], resource))
        resources.append((other["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    registry = Registry().with_resources(resources)
    return validators.validator_for(schema)(schema, registry=registry)


_POLICY_VALIDATOR = _validator("model_fetch_policy.schema.json")
_REQUEST_VALIDATOR = _validator("model_fetch_request.schema.json")
_RECORD_VALIDATOR = _validator("model_fetch_record.schema.json")
_REPORT_VALIDATOR = _validator("model_fetch_report.schema.json")


class ModelFetchError(Exception):
    """Any fetch-duty failure of shape, not of outcome: a bad
    policy, a request that fails its schema, a fetcher built
    without a store. Outcomes — refusals, unreachable sources,
    hash mismatches — are records, not exceptions."""


class FetchSourceError(Exception):
    """What a transport raises when the source cannot serve:
    the peer is unreachable, the named source is down, the
    locator names nothing there. The fetcher records it as
    ``engine_unreachable`` with the bytes received so far."""


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise ModelFetchError(f"{kind} fails its schema: {errors[0].message}")
    return record


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_fetch_policy(record: dict) -> dict:
    """One fetch policy against its contract schema."""
    return _validate(_POLICY_VALIDATOR, record, "model fetch policy")


def validate_fetch_request(record: dict) -> dict:
    """One fetch request against its contract schema."""
    return _validate(_REQUEST_VALIDATOR, record, "model fetch request")


def validate_fetch_record(record: dict) -> dict:
    """One fetch record against its contract schema."""
    return _validate(_RECORD_VALIDATOR, record, "model fetch record")


def default_policy() -> dict:
    """The starting policy, validated — a policy, not a fact."""
    return validate_fetch_policy(dict(DEFAULT_POLICY))


def _nearest_existing(path: Path) -> Path:
    """The closest existing ancestor of a path — disk space is
    asked of a directory that exists."""
    candidate = path
    while not candidate.exists():
        if candidate.parent == candidate:
            return candidate
        candidate = candidate.parent
    return candidate


class ModelFetcher:
    """One node's fetch duty: policy-gated, hash-verified
    fetching into the node's own model store.

    ``transport`` is the injected seam: a callable taking the
    validated request and returning an iterable of byte
    chunks, or an object with ``fetch_chunks(request)`` doing
    the same. It may raise :class:`FetchSourceError` when the
    source cannot serve. ``disk_free`` is injectable for the
    same reason the census probes are: the reserve check must
    be testable without filling a disk. ``timer`` measures
    run times; ``now`` stamps records; ``simulated`` is the
    honesty flag the builder sets — true whenever the
    transport is a fake, and carried into every record.
    """

    def __init__(
        self,
        node_id: str,
        store: ModelStore,
        *,
        policy: dict | None = None,
        transport=None,
        disk_free=None,
        timer=None,
        now=None,
        simulated: bool = False,
    ):
        if store is None:
            raise ModelFetchError("a fetcher needs the node's model store")
        self._node_id = node_id
        self._store = store
        self._policy = validate_fetch_policy(
            dict(policy) if policy is not None else default_policy()
        )
        self._transport = transport
        self._disk_free = disk_free or (
            lambda: shutil.disk_usage(_nearest_existing(self._store.root)).free
        )
        self._timer = timer or time.monotonic
        self._now = now or (lambda: datetime.now(UTC))
        self._simulated = bool(simulated)
        self._records: list[dict] = []
        self._events: list[dict] = []
        self._seq = 0

    # -- facts --------------------------------------------------------

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def policy(self) -> dict:
        return dict(self._policy)

    @property
    def records(self) -> list[dict]:
        return [dict(record) for record in self._records]

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    # -- the duty -------------------------------------------------------

    def fetch_model(self, request: dict) -> dict:
        """Carry out one fetch request and answer its record.

        Outcomes are data: fetched / already_present /
        refused / failed, each a validated record in the
        ledger. Only a request that fails its own schema
        raises — a malformed ask is a caller bug, not a
        fetch outcome."""
        request = validate_fetch_request(dict(request))
        started = self._timer()
        name = request["name"]

        entry = self._registered(name)
        if entry is not None and entry["sha256"] != request["expected_sha256"]:
            return self._record(
                request,
                started,
                status="refused",
                reason="name_registered_with_different_bytes",
                error_code="model_hash_mismatch",
                bytes_received=0,
                actual_sha256=None,
            )
        if entry is not None and self._store.is_present(name):
            return self._record(
                request,
                started,
                status="already_present",
                reason=None,
                error_code=None,
                bytes_received=0,
                actual_sha256=entry["sha256"],
            )

        refusal = self._policy_refusal(request)
        if refusal is not None:
            reason, error_code = refusal
            return self._record(
                request,
                started,
                status="refused",
                reason=reason,
                error_code=error_code,
                bytes_received=0,
                actual_sha256=None,
            )

        if self._transport is None:
            return self._record(
                request,
                started,
                status="failed",
                reason="no_transport_wired",
                error_code="engine_unreachable",
                bytes_received=0,
                actual_sha256=None,
            )

        staged, received, actual_sha256, failure = self._stream_to_staging(request)
        if failure is not None:
            reason, error_code = failure
            return self._record(
                request,
                started,
                status="failed",
                reason=reason,
                error_code=error_code,
                bytes_received=received,
                actual_sha256=actual_sha256,
            )
        assert staged is not None  # a clean stream always stages a file
        try:
            if received != request["expected_size_bytes"]:
                return self._record(
                    request,
                    started,
                    status="failed",
                    reason="size_mismatch",
                    error_code="model_hash_mismatch",
                    bytes_received=received,
                    actual_sha256=actual_sha256,
                )
            if actual_sha256 != request["expected_sha256"]:
                return self._record(
                    request,
                    started,
                    status="failed",
                    reason="hash_mismatch",
                    error_code="model_hash_mismatch",
                    bytes_received=received,
                    actual_sha256=actual_sha256,
                )
            try:
                self._store.add(
                    staged,
                    name=name,
                    family=request["family"],
                    quant=request["quant"],
                    format=request["format"],
                    capabilities=request["capabilities"],
                )
            except ModelStoreError as exc:
                return self._record(
                    request,
                    started,
                    status="failed",
                    reason=f"store_refused: {exc}",
                    error_code="internal",
                    bytes_received=received,
                    actual_sha256=actual_sha256,
                )
            verification = self._store.verify(name)
            if verification["status"] != "verified":
                return self._record(
                    request,
                    started,
                    status="failed",
                    reason="store_verification_failed",
                    error_code="verification_failed",
                    bytes_received=received,
                    actual_sha256=actual_sha256,
                )
            return self._record(
                request,
                started,
                status="fetched",
                reason=None,
                error_code=None,
                bytes_received=received,
                actual_sha256=actual_sha256,
            )
        finally:
            staged.unlink(missing_ok=True)

    def report(self) -> dict:
        """The duty's ledger as a validated report: totals
        that only rise, plus every attempt's record."""
        counts = {"fetched": 0, "already_present": 0, "refused": 0, "failed": 0}
        bytes_fetched = 0
        for record in self._records:
            counts[record["status"]] += 1
            if record["status"] == "fetched":
                bytes_fetched += record["bytes_received"]
        return _validate(
            _REPORT_VALIDATOR,
            {
                "version": 1,
                "node_id": self._node_id,
                "simulated": self._simulated,
                "counts": counts,
                "bytes_fetched": bytes_fetched,
                "records": self.records,
            },
            "model fetch report",
        )

    # -- internals ------------------------------------------------------

    def _registered(self, name: str) -> dict | None:
        try:
            return self._store.entry(name)
        except ModelStoreError:
            return None

    def _store_status(self, name: str) -> str:
        entry = self._registered(name)
        if entry is None:
            return "unregistered"
        return self._store.verify(name)["status"]

    def _policy_refusal(self, request: dict) -> tuple[str, str] | None:
        """The policy gates, in order — each checked before a
        single byte is asked for. None means the policy allows
        this fetch."""
        if request["source_kind"] == "peer" and not self._policy["allow_peer_fetch"]:
            return "peer_fetch_not_allowed", "scope_denied"
        if (
            request["source_kind"] == "named_source"
            and request["source_id"] not in self._policy["named_sources"]
        ):
            return "source_not_named_in_policy", "scope_denied"
        cap = self._policy["max_model_bytes"]
        if cap is not None and request["expected_size_bytes"] > cap:
            return "model_too_large_for_policy", "scope_denied"
        needed = request["expected_size_bytes"] + self._policy["min_free_disk_bytes"]
        if self._disk_free() < needed:
            return "insufficient_disk_reserve", "scope_denied"
        return None

    def _chunks(self, request: dict):
        """The transport's chunk stream, whichever of the two
        seam shapes the builder handed in."""
        transport = self._transport
        if hasattr(transport, "fetch_chunks"):
            return transport.fetch_chunks(request)
        return transport(request)

    def _stream_to_staging(self, request: dict):
        """Stream the source's chunks into the staging file,
        hashing as they land. Returns (staged_path,
        bytes_received, actual_sha256, failure) — on any
        failure the staging file is already removed and the
        hash is whatever the received bytes measured (None
        when nothing measurable arrived)."""
        safe = "".join(
            ch if ch.isalnum() or ch in "._-" else "_" for ch in request["name"]
        )
        staging_dir = self._store.root / _STAGING_DIR
        staged = staging_dir / f"{safe}.part"
        digest = hashlib.sha256()
        received = 0
        failure: tuple[str, str] | None = None
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
            with staged.open("wb") as handle:
                for chunk in self._chunks(request):
                    if not isinstance(chunk, bytes | bytearray):
                        failure = ("transport_sent_non_bytes", "engine_error")
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if received > request["expected_size_bytes"]:
                        failure = ("size_mismatch", "model_hash_mismatch")
                        break
        except FetchSourceError:
            failure = ("source_unreachable", "engine_unreachable")
        except Exception:  # noqa: BLE001 — a transport's own accidents are named, not hidden
            failure = ("transport_error", "engine_error")
        if failure is not None:
            staged.unlink(missing_ok=True)
            return None, received, digest.hexdigest() if received else None, failure
        return staged, received, digest.hexdigest(), None

    def _record(
        self,
        request: dict,
        started: float,
        *,
        status: str,
        reason: str | None,
        error_code: str | None,
        bytes_received: int,
        actual_sha256: str | None,
    ) -> dict:
        record = _validate(
            _RECORD_VALIDATOR,
            {
                "version": 1,
                "node_id": self._node_id,
                "model": request["name"],
                "source_kind": request["source_kind"],
                "source_id": request["source_id"],
                "status": status,
                "reason": reason,
                "error_code": error_code,
                "bytes_received": bytes_received,
                "actual_sha256": actual_sha256,
                "store_status": self._store_status(request["name"]),
                "run_ms": round((self._timer() - started) * 1000, 3),
                "simulated": self._simulated,
                "at": _fmt(self._now()),
            },
            "model fetch record",
        )
        self._records.append(record)
        event_type = {
            "fetched": "model.fetched",
            "already_present": "model.fetch_already_present",
            "refused": "model.fetch_refused",
            "failed": "model.fetch_failed",
        }[status]
        event = make_event(
            type=event_type,
            node_id=self._node_id,
            component="node_agent",
            payload={
                "model": request["name"],
                "source_kind": request["source_kind"],
                "source_id": request["source_id"],
                "status": status,
                "reason": reason,
                "error_code": error_code,
                "bytes_received": bytes_received,
                "simulated": self._simulated,
            },
            seq=self._seq,
            at=self._now(),
        )
        self._seq += 1
        self._events.append(event)
        return dict(record)


__all__ = [
    "DEFAULT_POLICY",
    "FetchSourceError",
    "ModelFetchError",
    "ModelFetcher",
    "default_policy",
    "validate_fetch_policy",
    "validate_fetch_record",
    "validate_fetch_request",
]
