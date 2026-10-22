from __future__ import annotations

import pytest

from lorebase_core.model import Kind, validate_schema
from lorebase_core.model.embedding import (
    Placeholder,
    TemplateError,
    kind_template,
    parse_template,
    template_problems,
)
from lorebase_core.model.yaml_schema import load_schema
from lorebase_core.validate import IssueCode

from .yaml_support import counting_factory

SCHEMA = """
domain: {id: supply}
kinds:
  Supplier:
    display_name: Supplier company
    primary_key: [code]
    title_attribute: legal_name
    attributes:
      code: {type: string, required: true}
      legal_name: {type: string, required: true}
      country: {type: string}
      score: {type: integer}
      active: {type: boolean}
      tags: {type: "array<string>"}
      tax_id: {type: string, sensitivity: [pii]}
    search:
      embedding_template: "{legal_name} ({country}) - {_kind} {_key}"
"""


def supplier(**search: object) -> Kind:
    schema = load_schema(SCHEMA, id_factory=counting_factory())
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    if search:
        kind = kind.model_copy(update={"search": kind.search.model_copy(update=search)})
    return kind


def ids(kind: Kind) -> dict[str, str]:
    return {a.api_name: a.id for a in kind.attributes.values()}


def test_parse_splits_literals_and_placeholders() -> None:
    template = parse_template("{{x}} { name }: {a}{b}")
    assert template.parts == ("{x} ", Placeholder("name"), ": ", Placeholder("a"), Placeholder("b"))
    assert template.fields == ("name", "a", "b")


@pytest.mark.parametrize("source", ["{a", "a}", "{}", "plain text", "{a{b}}"])
def test_parse_refuses_malformed_templates(source: str) -> None:
    with pytest.raises(TemplateError):
        parse_template(source)


def test_render_is_deterministic_and_collapses_whitespace() -> None:
    kind = supplier(embedding_template="{legal_name}\n\n  {tags}; {score}; {active}; {country}.")
    template = kind_template(kind)
    assert template is not None
    a = ids(kind)
    values = {a["legal_name"]: " Acme  GmbH ", a["tags"]: ["seals", "", "valves"], a["score"]: 7}
    values[a["active"]] = False
    assert template.render("S-1", values) == "Acme GmbH seals, valves; 7; false; ."


def test_special_fields_render_the_kind_label_and_the_key() -> None:
    kind = supplier()
    template = kind_template(kind)
    assert template is not None
    assert template.render("S-9", {ids(kind)["legal_name"]: "Acme"}) == (
        "Acme () - Supplier company S-9"
    )


def test_no_template_means_the_kind_is_not_embedded() -> None:
    assert kind_template(supplier(embedding_template=None)) is None
    assert kind_template(supplier(embedding_template="  ")) is None


def test_fingerprint_follows_what_changes_the_text() -> None:
    base = kind_template(supplier())
    assert base is not None
    same = kind_template(supplier())
    assert same is not None
    assert same.fingerprint == base.fingerprint
    other = kind_template(supplier(embedding_template="{legal_name}"))
    assert other is not None
    assert other.fingerprint != base.fingerprint
    renamed = supplier().model_copy(update={"display_name": "Vendor"})
    relabelled = kind_template(renamed)
    assert relabelled is not None
    assert relabelled.fingerprint != base.fingerprint


def test_unknown_derived_and_sensitive_attributes_are_refused() -> None:
    assert "not an Attribute" in template_problems(supplier(embedding_template="{nope}"))[0]
    sensitive = supplier(embedding_template="{legal_name} {tax_id}")
    assert "sensitive" in template_problems(sensitive)[0]
    allowed = supplier(embedding_template="{legal_name} {tax_id}", embedding_sensitive=True)
    assert template_problems(allowed) == []
    kind = supplier(embedding_template="{score}")
    score = kind.attribute_by_api_name("score")
    assert score is not None
    derived = kind.model_copy(
        update={
            "attributes": {
                **kind.attributes,
                score.id: score.model_copy(
                    update={"derivation_id": "drv_01JB0000000000000000000000"}
                ),
            }
        }
    )
    assert "derived" in template_problems(derived)[0]


def test_schema_validation_reports_an_invalid_template() -> None:
    def template_issues(text: str) -> list[tuple[str | int, ...]]:
        return [
            i.path
            for i in validate_schema(load_schema(text))
            if i.code is IssueCode.SCHEMA_EMBEDDING_TEMPLATE_INVALID
        ]

    [path] = template_issues(SCHEMA.replace("{_key}", "{missing}"))
    assert path[-2:] == ("search", "embedding_template")
    assert template_issues(SCHEMA) == []


def test_an_unset_sensitivity_switch_leaves_the_kind_hash_unchanged() -> None:
    kind = supplier()
    assert "embedding_sensitive" not in kind.content()["search"]  # type: ignore[operator]
