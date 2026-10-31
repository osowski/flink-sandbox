import unittest
import uuid

from dtm.common.ids import DTM_NAMESPACE, message_id, task_id


class IdsTest(unittest.TestCase):
    def test_message_id_is_uuid5_over_joined_parts(self):
        self.assertEqual(message_id("agent:risk", "c-1", "abc123", 0),
                         str(uuid.uuid5(DTM_NAMESPACE, "agent:risk|c-1|abc123|0")))

    def test_task_id_is_uuid5_over_joined_parts(self):
        self.assertEqual(task_id("agent:risk", "m-1"),
                         str(uuid.uuid5(DTM_NAMESPACE, "agent:risk|m-1")))

    def test_ids_differ_per_input(self):
        self.assertNotEqual(message_id("agent:risk", "c-1", "v", 0), message_id("agent:risk", "c-1", "v", 1))
        self.assertNotEqual(task_id("agent:risk", "m-1"), task_id("agent:enrolment", "m-1"))

    def test_namespace_literal(self):
        self.assertEqual(DTM_NAMESPACE, uuid.uuid5(uuid.NAMESPACE_URL, "urn:device-trust-mesh"))


if __name__ == "__main__":
    unittest.main()
