from __future__ import annotations
import copy
import unittest
from pathlib import Path
from scripts.dependency_policy import REQUIRED_CHECKS, TRUSTED_AUTHOR, eligible, permitted_change
from scripts.update_versions import UpdateError, extract_pins, replace_pins

BASE = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
PINS = extract_pins(BASE)
NEW_PINS = PINS | {'NGINX_VERSION': '1.30.6', 'NGINX_SHA256': 'f' * 64}
HEAD = replace_pins(BASE, NEW_PINS)
SHA = 'a' * 40


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.pr = {'state': 'OPEN', 'isDraft': False, 'author': {'login': TRUSTED_AUTHOR},
                   'baseRefName': 'master', 'headRefName': 'ci/update-pinned-versions',
                   'isCrossRepository': False, 'mergeStateStatus': 'CLEAN', 'headRefOid': SHA,
                   'labels': [{'name': 'dependencies'}, {'name': 'automated pr'}]}
        self.checks = [{'id': i, 'name': name, 'status': 'completed', 'conclusion': 'success',
                        'head_sha': SHA, 'app': {'slug': 'github-actions'}} for i, name in enumerate(sorted(REQUIRED_CHECKS), 1)]

    def evaluate(self, pr=None, checks=None, head=HEAD, files=None):
        return eligible(pr or self.pr, files if files is not None else ['Dockerfile'],
                        self.checks if checks is None else checks, BASE, head)

    def test_trusted_patch_after_all_checks(self):
        self.assertTrue(self.evaluate())

    def test_untrusted_metadata_and_files(self):
        for key, value in [('author', {'login': 'github-actions[bot]'}), ('state', 'CLOSED'),
                           ('isDraft', True), ('isCrossRepository', True), ('baseRefName', 'other'),
                           ('headRefName', 'attacker'), ('mergeStateStatus', 'BLOCKED'), ('labels', [])]:
            with self.subTest(key=key):
                self.assertFalse(self.evaluate(pr=self.pr | {key: value}))
        self.assertFalse(self.evaluate(files=['Dockerfile', 'scripts/dependency_policy.py']))

    def test_missing_skipped_cancelled_old_head_and_spoofed_checks(self):
        self.assertFalse(self.evaluate(checks=[]))
        for value in ['failure', 'cancelled', 'skipped', 'neutral', None]:
            checks = copy.deepcopy(self.checks)
            checks[0]['conclusion'] = value
            self.assertFalse(self.evaluate(checks=checks))
        for field, value in [('head_sha', 'b' * 40), ('status', 'in_progress'), ('app', {'slug': 'other'})]:
            checks = copy.deepcopy(self.checks)
            checks[0][field] = value
            self.assertFalse(self.evaluate(checks=checks))
        checks = self.checks + [self.checks[0] | {'id': 100, 'conclusion': 'failure'}]
        self.assertFalse(self.evaluate(checks=checks))

    def test_branch_moves_downgrades_code_changes_and_stale_checksum_blocked(self):
        for pins in [NEW_PINS | {'NGINX_VERSION': '1.32.0'}, NEW_PINS | {'ZLIB_VERSION': '1.2.0'},
                     NEW_PINS | {'NGINX_SHA256': PINS['NGINX_SHA256']},
                     PINS | {'NGINX_SHA256': 'e' * 64},
                     NEW_PINS | {'ALPINE_IMAGE': PINS['ALPINE_IMAGE'].replace('3.23', '3.24')}]:
            self.assertFalse(permitted_change(BASE, replace_pins(BASE, pins)))
        self.assertFalse(permitted_change(BASE, HEAD.replace('SIGQUIT', 'SIGTERM')))
        self.assertFalse(permitted_change(BASE, BASE))

    def test_invalid_and_duplicate_pins_rejected(self):
        with self.assertRaises(UpdateError):
            replace_pins(BASE, NEW_PINS | {'BROTLI_COMMIT': 'main'})
        with self.assertRaises(UpdateError):
            extract_pins(BASE + '\nENV NGINX_VERSION=1.30.5\n')

    def test_version_and_checksum_replaced_together(self):
        self.assertEqual(extract_pins(HEAD), NEW_PINS)
        self.assertIn('FROM ${ALPINE_IMAGE}', HEAD)


if __name__ == '__main__':
    unittest.main()
