import unittest
import os
import sys
import tempfile
import shutil
import fnmatch
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from uttale.backend import server
from uttale.backend.server import (
    resolve_db_path,
    pattern_to_wildcard,
    favorites_add,
    favorites_get,
    favorites_list,
    favorites_update,
    favorites_delete,
    parse_topic_time,
    read_topics,
    topics_dir_for,
    run_vtt_topics,
    start_topics_generation,
    _topics_running,
    _topics_lock,
    listens_upsert,
    listens_list,
    LISTENS_LIMIT,
    audio_etag,
    get_audio_segment,
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


class TestParseTopicTime(unittest.TestCase):
    def test_pads_missing_milliseconds(self):
        self.assertEqual(parse_topic_time('00:00:39'), '00:00:39.000')

    def test_passes_through_milliseconds(self):
        self.assertEqual(parse_topic_time('00:00:39.240'), '00:00:39.240')

    def test_normalizes_single_digit_fields(self):
        self.assertEqual(parse_topic_time('1:2:3'), '01:02:03.000')

    def test_rejects_non_timestamp(self):
        self.assertIsNone(parse_topic_time('Velkommen'))

    def test_rejects_out_of_range_minutes(self):
        self.assertIsNone(parse_topic_time('00:99:00'))


class TestReadTopics(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.filename = os.path.join('48k', 'Pod', '20260623', 'by10m', 'by10m_00.vtt')
        self.episode_dir = os.path.join(self.root, os.path.dirname(self.filename))
        os.makedirs(self.episode_dir)
        self.topics_path = os.path.join(self.episode_dir, 'topics')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, text):
        with open(self.topics_path, 'w', encoding='utf-8') as f:
            f.write(text)

    def test_missing_file_returns_empty(self):
        self.assertEqual(read_topics(self.root, self.filename), [])

    def test_parses_lines(self):
        self.write('00:00:39 Velkommen tilbake\n00:00:58 Kevins jobb\n')
        topics = read_topics(self.root, self.filename)
        self.assertEqual(len(topics), 2)
        self.assertEqual(topics[0].start, '00:00:39.000')
        self.assertEqual(topics[0].title, 'Velkommen tilbake')
        self.assertEqual(topics[1].start, '00:00:58.000')

    def test_title_with_colons_preserved(self):
        self.write('00:03:47 Rondane: pakking 10:00 sekk\n')
        topics = read_topics(self.root, self.filename)
        self.assertEqual(topics[0].start, '00:03:47.000')
        self.assertEqual(topics[0].title, 'Rondane: pakking 10:00 sekk')

    def test_skips_blank_and_malformed_lines(self):
        self.write('\n00:00:39 ok\nno timestamp here\n   \nbad:time:x junk\n00:01:00 also ok\n')
        topics = read_topics(self.root, self.filename)
        self.assertEqual([t.start for t in topics], ['00:00:39.000', '00:01:00.000'])

    def test_skips_timestamp_only_line(self):
        self.write('00:00:39\n00:00:58 has title\n')
        topics = read_topics(self.root, self.filename)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].title, 'has title')


class TestGenerateTopics(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.logs = tempfile.mkdtemp()
        self.bindir = tempfile.mkdtemp()
        self.filename = os.path.join('48k', 'Pod', '20260623', 'by10m', 'by10m_00.vtt')
        self.episode_dir = os.path.join(self.root, os.path.dirname(self.filename))
        os.makedirs(self.episode_dir)
        self.topics_path = os.path.join(self.episode_dir, 'topics')
        self._orig_path = os.environ.get('PATH', '')

    def tearDown(self):
        os.environ['PATH'] = self._orig_path
        with _topics_lock:
            _topics_running.discard(os.path.realpath(self.episode_dir))
        for d in (self.root, self.logs, self.bindir):
            shutil.rmtree(d, ignore_errors=True)

    def stub(self, body):
        path = os.path.join(self.bindir, 'vtt-topics')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body)
        os.chmod(path, 0o755)
        os.environ['PATH'] = self.bindir + os.pathsep + self._orig_path

    def test_dir_for_resolves_episode_directory(self):
        self.assertEqual(topics_dir_for(self.root, self.filename), self.episode_dir)

    def test_run_publishes_topics_on_success(self):
        self.stub('#!/bin/sh\nprintf "00:00:10 Intro\\n00:01:00 Body\\n"\n')
        code = run_vtt_topics(self.episode_dir, log_dir=self.logs)
        self.assertEqual(code, 0)
        with open(self.topics_path, encoding='utf-8') as f:
            self.assertIn('00:00:10 Intro', f.read())

    def test_run_writes_log_file(self):
        self.stub('#!/bin/sh\nprintf "00:00:10 Intro\\n"\n')
        run_vtt_topics(self.episode_dir, log_dir=self.logs)
        self.assertTrue(any(n.endswith('.log') for n in os.listdir(self.logs)))

    def test_run_keeps_existing_topics_on_failure(self):
        with open(self.topics_path, 'w', encoding='utf-8') as f:
            f.write('00:00:05 Keep me\n')
        self.stub('#!/bin/sh\necho boom >&2\nexit 3\n')
        code = run_vtt_topics(self.episode_dir, log_dir=self.logs)
        self.assertEqual(code, 3)
        with open(self.topics_path, encoding='utf-8') as f:
            self.assertEqual(f.read(), '00:00:05 Keep me\n')

    def test_run_does_not_publish_empty_output(self):
        self.stub('#!/bin/sh\nexit 0\n')
        run_vtt_topics(self.episode_dir, log_dir=self.logs)
        self.assertFalse(os.path.exists(self.topics_path))

    def test_start_returns_not_found_for_missing_dir(self):
        missing = os.path.join('48k', 'Nope', '20260101', 'by10m', 'x_00.vtt')
        self.assertEqual(start_topics_generation(self.root, missing), 'not found')

    def test_start_returns_already_running_when_locked(self):
        key = os.path.realpath(self.episode_dir)
        with _topics_lock:
            _topics_running.add(key)
        try:
            self.assertEqual(start_topics_generation(self.root, self.filename), 'already running')
        finally:
            with _topics_lock:
                _topics_running.discard(key)

    def test_start_runs_and_publishes(self):
        self.stub('#!/bin/sh\nprintf "00:00:10 Intro\\n"\n')
        status = start_topics_generation(self.root, self.filename, log_dir=self.logs)
        self.assertEqual(status, 'started')
        for _ in range(50):
            if os.path.exists(self.topics_path):
                break
            time.sleep(0.05)
        with open(self.topics_path, encoding='utf-8') as f:
            self.assertIn('00:00:10 Intro', f.read())
        self.assertNotIn(os.path.realpath(self.episode_dir), _topics_running)


class TestAudioCaching(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.filename = os.path.join('48k', 'Pod', '20260628', 'by10m', 'by10m_00.vtt')
        ogg = os.path.join(self.root, os.path.dirname(self.filename), 'by10m_00.ogg')
        os.makedirs(os.path.dirname(ogg))
        subprocess.run(
            ['ffmpeg', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono',
             '-t', '2', '-c:a', 'libopus', ogg],
            capture_output=True, check=True,
        )
        self._orig_args = server.args
        server.args = SimpleNamespace(root=self.root)

    def tearDown(self):
        server.args = self._orig_args
        shutil.rmtree(self.root, ignore_errors=True)

    def test_etag_is_stable_for_a_span(self):
        a = audio_etag(self.filename, '00:00:00.000', '00:00:01.000')
        b = audio_etag(self.filename, '00:00:00.000', '00:00:01.000')
        self.assertEqual(a, b)
        self.assertTrue(a.startswith('"') and a.endswith('"'))

    def test_etag_differs_across_spans(self):
        a = audio_etag(self.filename, '00:00:00.000', '00:00:01.000')
        b = audio_etag(self.filename, '00:00:00.000', '00:00:01.500')
        self.assertNotEqual(a, b)

    def test_segment_headers_include_etag_and_immutable(self):
        _data, headers = get_audio_segment(self.filename, '00:00:00.000', '00:00:01.000')
        self.assertEqual(headers['ETag'], audio_etag(self.filename, '00:00:00.000', '00:00:01.000'))
        self.assertIn('immutable', headers['Cache-Control'])


class TestListens(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, 'listens.db')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_upsert_inserts_row(self):
        row = listens_upsert(self.db, 'a/p/d/x_00.vtt', '00:01:00.000')
        self.assertEqual(row['filename'], 'a/p/d/x_00.vtt')
        self.assertEqual(row['position'], '00:01:00.000')
        self.assertTrue(row['updated_at'])

    def test_upsert_updates_position_not_duplicate(self):
        listens_upsert(self.db, 'a/p/d/x_00.vtt', '00:01:00.000')
        row = listens_upsert(self.db, 'a/p/d/x_00.vtt', '00:05:00.000')
        self.assertEqual(row['position'], '00:05:00.000')
        self.assertEqual(len(listens_list(self.db)), 1)

    def test_list_newest_first(self):
        listens_upsert(self.db, 'a/p/d/one_00.vtt', '00:00:10.000')
        time.sleep(0.01)
        listens_upsert(self.db, 'a/p/d/two_00.vtt', '00:00:20.000')
        rows = listens_list(self.db)
        self.assertEqual(rows[0]['filename'], 'a/p/d/two_00.vtt')
        self.assertEqual(rows[1]['filename'], 'a/p/d/one_00.vtt')

    def test_prune_keeps_only_limit_most_recent(self):
        for i in range(LISTENS_LIMIT + 5):
            listens_upsert(self.db, f'a/p/d/ep{i:02d}_00.vtt', '00:00:01.000')
            time.sleep(0.005)
        rows = listens_list(self.db)
        self.assertEqual(len(rows), LISTENS_LIMIT)
        names = {r['filename'] for r in rows}
        self.assertNotIn('a/p/d/ep00_00.vtt', names)
        self.assertIn(f'a/p/d/ep{LISTENS_LIMIT + 4:02d}_00.vtt', names)

    def test_wal_mode_enabled(self):
        listens_upsert(self.db, 'a/p/d/x_00.vtt', '00:01:00.000')
        conn = sqlite3.connect(self.db)
        mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
        conn.close()
        self.assertEqual(mode.lower(), 'wal')


if __name__ == '__main__':
    unittest.main()