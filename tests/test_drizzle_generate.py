"""Tests drizzle-generate.py against the repo's pinned drizzle-kit in a scratch project.

Run: DRIZZLE_NODE_MODULES=<repo>/packages/db/node_modules python3 -m unittest tests/test_drizzle_generate.py -v
"""
import glob
import os
import shutil
import subprocess
import tempfile
import unittest

HELPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'drizzle-generate.py')
REPO_DB_MODULES = os.environ.get('DRIZZLE_NODE_MODULES', '')  # a repo's packages/db/node_modules with drizzle-kit
HEADER = "import { pgTable, text, integer } from 'drizzle-orm/pg-core';\n"


def table(name, *columns):
    fields = ', '.join(f"{column}: text('{column}')" for column in columns)

    return f"export const {name} = pgTable('{name}', {{ id: integer('id').primaryKey(), {fields} }});\n"


@unittest.skipUnless(os.path.isdir(REPO_DB_MODULES), 'set DRIZZLE_NODE_MODULES to a node_modules with drizzle-kit')
class DrizzleGenerate(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp(prefix='drizzle-generate-test-')
        os.symlink(REPO_DB_MODULES, os.path.join(self.project, 'node_modules'))

        with open(os.path.join(self.project, 'drizzle.config.ts'), 'w') as handle:
            handle.write("export default { dialect: 'postgresql', schema: './schema.ts', out: './out' };\n")

    def tearDown(self):
        shutil.rmtree(self.project, ignore_errors=True)

    def schema(self, *tables):
        with open(os.path.join(self.project, 'schema.ts'), 'w') as handle:
            handle.write(HEADER + ''.join(tables))

    def generate(self, *answers):
        return subprocess.run(['python3', HELPER, *answers], cwd=self.project, capture_output=True, text=True, timeout=240)

    def migrations(self):
        return sorted(glob.glob(os.path.join(self.project, 'out', '*.sql')))

    def latest_sql(self):
        with open(self.migrations()[-1]) as handle:
            return handle.read()

    def test_no_prompt_generates(self):
        self.schema(table('t', 'a'))
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.migrations()), 1)

    def test_two_column_renames_in_two_tables(self):
        self.schema(table('t', 'a'), table('u', 'x'))
        self.generate()
        self.schema(table('t', 'b'), table('u', 'y'))
        result = self.generate('column:t.b=a', 'column:u.y=x')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        sql = self.latest_sql()
        self.assertIn('ALTER TABLE "t" RENAME COLUMN "a" TO "b"', sql)
        self.assertIn('ALTER TABLE "u" RENAME COLUMN "x" TO "y"', sql)

    def test_table_rename(self):
        self.schema(table('old_name', 'a'))
        self.generate()
        self.schema(table('new_name', 'a'))
        result = self.generate('table:new_name=old_name')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('ALTER TABLE "old_name" RENAME TO "new_name"', self.latest_sql())

    def test_explicit_create_drops_and_adds(self):
        self.schema(table('t', 'a'))
        self.generate()
        self.schema(table('t', 'b'))
        result = self.generate('column:t.b=create')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        sql = self.latest_sql()
        self.assertIn('DROP COLUMN "a"', sql)
        self.assertIn('ADD COLUMN "b"', sql)

    def test_unchanged_schema_succeeds(self):
        self.schema(table('t', 'a'))
        self.generate()
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('No schema changes', result.stdout)
        self.assertEqual(len(self.migrations()), 1)

    def test_swallowed_error_fails_closed(self):
        with open(os.path.join(self.project, 'schema.ts'), 'w') as handle:
            handle.write(HEADER + "throw new Error('schema failed to load');\n")

        result = self.generate()
        self.assertEqual(result.returncode, 6, result.stderr + result.stdout)
        self.assertIn('schema failed to load', result.stdout)
        self.assertIn('exited 0 without a result', result.stderr)
        self.assertEqual(self.migrations(), [])

    def test_unanswered_prompt_fails_closed(self):
        self.schema(table('t', 'a'))
        self.generate()
        self.schema(table('t', 'b'))
        result = self.generate()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("unanswered prompt 'column:t.b'", result.stderr)
        self.assertEqual(len(self.migrations()), 1)

    def test_answer_without_matching_option_fails_closed(self):
        self.schema(table('t', 'a'))
        self.generate()
        self.schema(table('t', 'b'))
        result = self.generate('column:t.b=nope')
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(len(self.migrations()), 1)

    def test_custom_creates_an_empty_named_migration(self):
        self.schema(table('t', 'a'))
        self.generate()
        result = self.generate('--custom', '--name', 'backfill_a')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(self.migrations()), 2)
        self.assertTrue(self.migrations()[-1].endswith('0001_backfill_a.sql'), self.migrations())
        self.assertNotIn('CREATE TABLE', self.latest_sql())

    def test_name_names_a_generated_migration(self):
        self.schema(table('t', 'a'))
        result = self.generate('--name=first_table')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(self.migrations()[-1].endswith('0000_first_table.sql'), self.migrations())

    def test_other_flags_are_refused(self):
        self.schema(table('t', 'a'))
        result = self.generate('--force')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported flag '--force'", result.stderr)
        self.assertEqual(self.migrations(), [])


if __name__ == '__main__':
    unittest.main()
