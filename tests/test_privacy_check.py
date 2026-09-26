"""Publication checks use exact staged content and never print matched values."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('privacy_check', Path(__file__).resolve().parents[1] / 'tools/privacy_check.py')
audit = importlib.util.module_from_spec(spec); spec.loader.exec_module(audit)


class PrivacyCheckTests(unittest.TestCase):
    def test_real_format_token_is_redacted_but_policy_identifier_is_not_a_key(self):
        token = 'sk-' + 'a' * 32
        result = audit.inspect_content('example.py', token.encode())
        self.assertEqual(result[0]['kind'], 'credential_like_token')
        self.assertNotIn(token, json.dumps(result))
        self.assertEqual(audit.inspect_content('policy.py', b'task-scoped-native-observations-v1.1'), [])

    def test_personal_path_and_email_require_review_but_synthetic_values_pass(self):
        personal = '/Users/' + 'real-account' + '/notes'
        email = 'person' + '@' + 'mail.invalid-provider.com'
        rows = audit.inspect_content('example.py', (personal + '\n' + email).encode())
        self.assertEqual([r['kind'] for r in rows], ['literal_personal_home_path', 'non_example_email_requires_review'])
        self.assertNotIn(personal, json.dumps(rows)); self.assertNotIn(email, json.dumps(rows))
        self.assertEqual(audit.inspect_content('fixture.py', b'/Users/private/document shipping@example.test'), [])

    def test_archive_embedded_private_content_is_scanned_without_extraction(self):
        import zipfile
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('docProps/core.xml', '<dc:creator>Example person</dc:creator>')
        result = audit.inspect_content('example.xlsx', out.getvalue())
        self.assertEqual(result[0]['kind'], 'document_author_metadata_requires_review')
        self.assertNotIn('Example person', json.dumps(result))

    def test_binary_and_private_file_classes_are_not_silently_accepted(self):
        self.assertEqual(audit.inspect_content('image.png', b'\x89PNG')[0]['kind'], 'binary_requires_manual_review')
        self.assertEqual(audit.inspect_content('.env', b'')[0]['kind'], 'private_file_class')
        self.assertEqual(audit.inspect_content('artifacts/trace.json', b'{}')[0]['kind'], 'private_file_class')

    def test_exact_index_catches_secret_even_if_working_file_was_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', folder], check=True)
            token = 'sk-' + 'b' * 32
            (root / 'source.py').write_text(token)
            subprocess.run(['git', '-C', folder, 'add', 'source.py'], check=True)
            (root / 'source.py').write_text('clean')
            self.assertTrue(audit.scan(root)['passed'])
            output = io.StringIO()
            with redirect_stdout(output):
                code = audit.main(['--root', folder, '--staged'])
            self.assertEqual(code, 1)
            self.assertNotIn(token, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())['findings'][0]['kind'], 'credential_like_token')
