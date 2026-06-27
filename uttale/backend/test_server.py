import unittest
import os
import sys
import tempfile
import shutil
import fnmatch
import sqlite3
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from uttale.backend.server import (
    resolve_db_path,
    pattern_to_wildcard,
    favorites_add,
    favorites_get,
    favorites_list,
    favorites_update,
    favorites_delete,
)


class TestDatabasePathResolution(unittest.TestCase):
    def setUp(self):
        self.test_cache_dir = tempfile.mkdtemp()
        self.original_home = os.environ.get('HOME')
        os.environ['HOME'] = self.test_cache_dir

    def tearDown(self):
        if self.original_home:
            os.environ['HOME'] = self.original_home
        if os.path.exists(self.test_cache_dir):
            shutil.rmtree(self.test_cache_dir)

    def test_simple_filename_goes_to_cache(self):
        path = resolve_db_path('202510.db')
        expected = os.path.join(self.test_cache_dir, '.cache', 'srst-uttale', '202510.db')
        self.assertEqual(path, expected)
        self.assertTrue(os.path.exists(os.path.dirname(path)))

    def test_another_simple_filename(self):
        path = resolve_db_path('my_data.db')
        expected = os.path.join(self.test_cache_dir, '.cache', 'srst-uttale', 'my_data.db')
        self.assertEqual(path, expected)

    def test_relative_path_with_dot_slash(self):
        path = resolve_db_path('./test.db')
        self.assertEqual(path, './test.db')

    def test_relative_path_with_parent_dir(self):
        path = resolve_db_path('../data/test.db')
        self.assertEqual(path, '../data/test.db')

    def test_absolute_path(self):
        path = resolve_db_path('/tmp/line.db')
        self.assertEqual(path, '/tmp/line.db')

    def test_tilde_path(self):
        path = resolve_db_path('~/mydata.db')
        self.assertEqual(path, '~/mydata.db')

    def test_path_with_subdirectory(self):
        path = resolve_db_path('subdir/test.db')
        self.assertEqual(path, 'subdir/test.db')

    def test_cache_directory_creation(self):
        cache_dir = os.path.join(self.test_cache_dir, '.cache', 'srst-uttale')
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir)
        self.assertFalse(os.path.exists(cache_dir))
        resolve_db_path('testfile.db')
        self.assertTrue(os.path.exists(cache_dir))


class TestWildcardPatterns(unittest.TestCase):
    def test_empty_pattern(self):
        self.assertEqual(pattern_to_wildcard(''), '*')

    def test_whitespace_only_pattern(self):
        self.assertEqual(pattern_to_wildcard('   '), '*')
        self.assertEqual(pattern_to_wildcard('\t\n'), '*')

    def test_simple_pattern(self):
        self.assertEqual(pattern_to_wildcard('202510'), '*202510*')

    def test_pattern_with_two_words(self):
        self.assertEqual(pattern_to_wildcard('202510 kontakt'), '*202510*kontakt*')

    def test_pattern_with_three_words(self):
        self.assertEqual(pattern_to_wildcard('a b c'), '*a*b*c*')

    def test_pattern_with_extra_spaces(self):
        self.assertEqual(pattern_to_wildcard('  word1   word2  '), '*word1*word2*')

    def test_pattern_is_lowercase(self):
        result = pattern_to_wildcard('UPPER Case')
        self.assertEqual(result, '*upper*case*')

    def test_pattern_with_single_word_and_spaces(self):
        self.assertEqual(pattern_to_wildcard('  single  '), '*single*')


class TestPatternFiltering(unittest.TestCase):
    def setUp(self):
        self.test_files = [
            '2025/202510_kontakt.vtt',
            '2025/202510_rapport.vtt',
            '2024/202410_kontakt.vtt',
            'archive/old_kontakt.vtt',
            'misc/test.vtt'
        ]

    def test_filter_with_two_terms(self):
        pattern = '202510 kontakt'
        wildcard = pattern_to_wildcard(pattern)
        filtered = [f for f in self.test_files if fnmatch.fnmatch(f.lower(), wildcard)]
        self.assertEqual(filtered, ['2025/202510_kontakt.vtt'])

    def test_filter_single_term_multiple_matches(self):
        pattern = 'kontakt'
        wildcard = pattern_to_wildcard(pattern)
        filtered = [f for f in self.test_files if fnmatch.fnmatch(f.lower(), wildcard)]
        self.assertEqual(len(filtered), 3)
        self.assertIn('2025/202510_kontakt.vtt', filtered)
        self.assertIn('2024/202410_kontakt.vtt', filtered)
        self.assertIn('archive/old_kontakt.vtt', filtered)

    def test_filter_by_year(self):
        pattern = '2025'
        wildcard = pattern_to_wildcard(pattern)
        filtered = [f for f in self.test_files if fnmatch.fnmatch(f.lower(), wildcard)]
        self.assertEqual(len(filtered), 2)
        self.assertIn('2025/202510_kontakt.vtt', filtered)
        self.assertIn('2025/202510_rapport.vtt', filtered)

    def test_filter_no_matches(self):
        pattern = 'nonexistent'
        wildcard = pattern_to_wildcard(pattern)
        filtered = [f for f in self.test_files if fnmatch.fnmatch(f.lower(), wildcard)]
        self.assertEqual(len(filtered), 0)

    def test_filter_matches_all(self):
        pattern = ''
        wildcard = pattern_to_wildcard(pattern)
        filtered = [f for f in self.test_files if fnmatch.fnmatch(f.lower(), wildcard)]
        self.assertEqual(len(filtered), len(self.test_files))


class TestFavorites(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, 'favorites.db')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_schema_created_on_first_use(self):
        favorites_list(self.db)
        conn = sqlite3.connect(self.db)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        self.assertIn('favorites', tables)

    def test_add_returns_full_row(self):
        fav = favorites_add(self.db, 'a.vtt', '00:00:01.000', '00:00:02.000', 'hi', 'note')
        self.assertEqual(fav['filename'], 'a.vtt')
        self.assertEqual(fav['start'], '00:00:01.000')
        self.assertEqual(fav['end'], '00:00:02.000')
        self.assertEqual(fav['text'], 'hi')
        self.assertEqual(fav['comment'], 'note')
        self.assertTrue(fav['created_at'])
        self.assertEqual(fav['created_at'], fav['updated_at'])
        self.assertIsNone(fav['exported_at'])

    def test_add_defaults_empty_comment(self):
        fav = favorites_add(self.db, 'a.vtt', '00:00:01.000', '00:00:02.000', 'hi')
        self.assertEqual(fav['comment'], '')

    def test_get_missing_returns_none(self):
        self.assertIsNone(favorites_get(self.db, 'nope.vtt', '00:00:01.000'))

    def test_upsert_preserves_created_at_and_updates_fields(self):
        first = favorites_add(self.db, 'a.vtt', '00:00:01.000', '00:00:02.000', 'hi', 'one')
        second = favorites_add(self.db, 'a.vtt', '00:00:01.000', '00:00:03.000', 'hello', 'two')
        self.assertEqual(second['created_at'], first['created_at'])
        self.assertEqual(second['end'], '00:00:03.000')
        self.assertEqual(second['text'], 'hello')
        self.assertEqual(second['comment'], 'two')
        rows = favorites_list(self.db)
        self.assertEqual(len(rows), 1)

    def test_list_default_sort_is_created_desc(self):
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '', '', '')
        time.sleep(0.01)
        favorites_add(self.db, 'b.vtt', '00:00:02.000', '', '', '')
        time.sleep(0.01)
        favorites_add(self.db, 'c.vtt', '00:00:03.000', '', '', '')
        keys = [r['filename'] for r in favorites_list(self.db)]
        self.assertEqual(keys, ['c.vtt', 'b.vtt', 'a.vtt'])

    def test_list_sort_created_asc(self):
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '', '', '')
        time.sleep(0.01)
        favorites_add(self.db, 'b.vtt', '00:00:02.000', '', '', '')
        keys = [r['filename'] for r in favorites_list(self.db, sort='created_asc')]
        self.assertEqual(keys, ['a.vtt', 'b.vtt'])

    def test_list_sort_name_asc(self):
        favorites_add(self.db, 'b.vtt', '00:00:01.000', '', '', '')
        favorites_add(self.db, 'a.vtt', '00:00:09.000', '', '', '')
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '', '', '')
        keys = [(r['filename'], r['start']) for r in favorites_list(self.db, sort='name_asc')]
        self.assertEqual(keys, [
            ('a.vtt', '00:00:01.000'),
            ('a.vtt', '00:00:09.000'),
            ('b.vtt', '00:00:01.000'),
        ])

    def test_list_sort_name_desc(self):
        favorites_add(self.db, 'b.vtt', '00:00:01.000', '', '', '')
        favorites_add(self.db, 'a.vtt', '00:00:09.000', '', '', '')
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '', '', '')
        keys = [(r['filename'], r['start']) for r in favorites_list(self.db, sort='name_desc')]
        self.assertEqual(keys, [
            ('b.vtt', '00:00:01.000'),
            ('a.vtt', '00:00:09.000'),
            ('a.vtt', '00:00:01.000'),
        ])

    def test_list_unknown_sort_falls_back_to_created_desc(self):
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '', '', '')
        time.sleep(0.01)
        favorites_add(self.db, 'b.vtt', '00:00:02.000', '', '', '')
        keys = [r['filename'] for r in favorites_list(self.db, sort='bogus')]
        self.assertEqual(keys, ['b.vtt', 'a.vtt'])

    def test_list_filtered_by_filename(self):
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '', '', '')
        favorites_add(self.db, 'b.vtt', '00:00:01.000', '', '', '')
        rows = favorites_list(self.db, 'a.vtt')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['filename'], 'a.vtt')

    def test_update_comment_changes_updated_at_only(self):
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '00:00:02.000', 'hi', 'one')
        updated = favorites_update(self.db, 'a.vtt', '00:00:01.000', 'changed')
        self.assertEqual(updated['comment'], 'changed')
        self.assertEqual(updated['text'], 'hi')

    def test_update_missing_returns_none(self):
        self.assertIsNone(favorites_update(self.db, 'nope.vtt', '00:00:01.000', 'x'))

    def test_update_set_exported_stamps_exported_at(self):
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '00:00:02.000', 'hi', 'one')
        updated = favorites_update(self.db, 'a.vtt', '00:00:01.000', set_exported=True)
        self.assertTrue(updated['exported_at'])
        self.assertEqual(updated['comment'], 'one')

    def test_update_set_exported_preserves_comment_when_omitted(self):
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '00:00:02.000', 'hi', 'keep')
        favorites_update(self.db, 'a.vtt', '00:00:01.000', set_exported=True)
        row = favorites_get(self.db, 'a.vtt', '00:00:01.000')
        self.assertEqual(row['comment'], 'keep')
        self.assertTrue(row['exported_at'])

    def test_update_comment_does_not_set_exported(self):
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '00:00:02.000', 'hi', 'one')
        updated = favorites_update(self.db, 'a.vtt', '00:00:01.000', 'changed')
        self.assertIsNone(updated['exported_at'])

    def test_delete_existing_returns_true(self):
        favorites_add(self.db, 'a.vtt', '00:00:01.000', '', '', '')
        self.assertTrue(favorites_delete(self.db, 'a.vtt', '00:00:01.000'))
        self.assertEqual(favorites_list(self.db), [])

    def test_delete_missing_returns_false(self):
        self.assertFalse(favorites_delete(self.db, 'nope.vtt', '00:00:01.000'))


if __name__ == '__main__':
    unittest.main()