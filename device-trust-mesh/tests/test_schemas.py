import unittest

from jsonschema import Draft202012Validator

from dtm.common.schemas import (
    SCHEMA_DIR,
    UnknownSchema,
    a2a_validator,
    load_a2a_schema,
    load_avro_schema,
)


class SchemasTest(unittest.TestCase):
    def test_all_subjects_vendored(self):
        subjects = {p.stem for p in (SCHEMA_DIR / "avro").glob("*.avsc")}
        self.assertEqual(len(subjects), 15)
        self.assertIn("device.events-value", subjects)
        self.assertEqual({p.stem for p in (SCHEMA_DIR / "a2a").glob("*.json")},
            {"agent.cards", "agent.task-events", "agent.tasks.enrolment",
             "agent.tasks.revocation", "agent.tasks.risk", "agent.tasks.step-up"})

    def test_load_avro_schema_parses(self):
        self.assertEqual(load_avro_schema("device.events-value")["name"], "DeviceEvent")

    def test_a2a_validator_is_2020_12_and_cached(self):
        validator = a2a_validator("agent.tasks.risk")
        self.assertIsInstance(validator, Draft202012Validator)
        self.assertIs(validator, a2a_validator("agent.tasks.risk"))

    def test_unknown_schema_raises(self):
        with self.assertRaises(UnknownSchema):
            load_a2a_schema("device.events")


if __name__ == "__main__":
    unittest.main()
