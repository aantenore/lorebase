"""What a caller supplies to evaluate: the clock, Records, the principal, the budget."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from lorebase_core.expr.ast import STEP_VARIABLE
from lorebase_core.expr.environment import KEY, LINKS, PARAMS, PRINCIPAL, Environment
from lorebase_core.expr.errors import evaluation_error
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.resolver import RecordResolver
from lorebase_core.expr.values import LinksValue, ObjectValue, RecordValue, Ref, to_runtime
from lorebase_core.model.record import Record


@dataclass(frozen=True, slots=True)
class Principal:
    """The caller, as expressions see it: ``principal.id`` and ``principal.subjects``."""

    id: str
    subjects: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationContext:
    """Inputs of one evaluation besides the expression's own variables.

    ``now`` is the clock (timezone-aware); ``resolver`` answers ``record()`` and ``links``;
    ``limits`` overrides the limits the expression was compiled with; ``mint_key`` mints
    keys of Records created by steps of Kinds without a natural key.
    """

    now: datetime
    resolver: RecordResolver | None = None
    principal: Principal | None = None
    limits: ExprLimits | None = None
    mint_key: Callable[[], str] | None = None


def activation(
    environment: Environment,
    *,
    record: Record | None = None,
    parameters: Mapping[str, JsonValue] | None = None,
    principal: Principal | None = None,
    steps: Sequence[Ref | None] | None = None,
) -> dict[str, object]:
    """Runtime variables of ``environment`` from canonical inputs.

    ``record`` is required when the environment has a subject Kind (its Attributes are
    variables); ``parameters`` are canonical parameter values by name.
    """
    variables: dict[str, object] = {}
    if environment.subject is not None:
        if record is None or record.kind != environment.subject:
            name = environment.kind_name(environment.subject)
            raise evaluation_error(f"this expression needs a Record of Kind {name}")
        kind = environment.schema.kinds[record.kind]
        subject = RecordValue(kind, record.key, record.values)
        for attribute in kind.attributes.values():
            variables[attribute.api_name] = subject.get(attribute.api_name)
        variables[KEY] = record.key
        variables[LINKS] = LinksValue(kind, record.key)
    if PARAMS in environment.objects:
        given = parameters or {}
        values = {
            name: to_runtime(spec, given.get(name)) for name, spec in environment.parameters.items()
        }
        variables.update(values)
        variables[PARAMS] = ObjectValue(PARAMS, values)
        variables[PRINCIPAL] = (
            None
            if principal is None
            else ObjectValue(PRINCIPAL, {"id": principal.id, "subjects": list(principal.subjects)})
        )
    if steps is not None:
        variables[STEP_VARIABLE] = list(steps)
    return variables
