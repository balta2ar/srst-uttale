import unittest
import os
import sys
import tempfile
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from uttale.backend import server
from uttale.backend.server import (
    resolve_db_path,
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


class TestProcessVtt(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.rel = os.path.join('48k', 'Pod', '20260623', 'by10m', 'by10m_00.vtt')
        self.abs = os.path.join(self.root, self.rel)
        os.makedirs(os.path.dirname(self.abs))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_vtt(self, body):
        with open(self.abs, 'w', encoding='utf-8') as f:
            f.write(body)

    def test_parses_captions_to_tuples(self):
        self.write_vtt(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhei der\n\n"
            "00:00:01.000 --> 00:00:02.500\nandre linje\n"
        )
        rows = server.process_vtt(self.rel, self.root)
        self.assertEqual(rows[0], (self.rel, '00:00:00.000', '00:00:01.000', 'hei der'))
        self.assertEqual(rows[1][3], 'andre linje')

    def test_missing_file_returns_empty(self):
        rows = server.process_vtt(os.path.join('48k', 'X', '20200101', 'a', 'b.vtt'), self.root)
        self.assertEqual(rows, [])


class TestPatternToFdRegex(unittest.TestCase):
    def test_single_token(self):
        self.assertEqual(server.pattern_to_fd_regex('idioti'), '(?i)idioti')

    def test_multiple_tokens_in_order(self):
        self.assertEqual(server.pattern_to_fd_regex('idioti 202606'), '(?i)idioti.*202606')

    def test_escapes_regex_special_chars(self):
        self.assertEqual(server.pattern_to_fd_regex('c++ a.b'), r'(?i)c\+\+.*a\.b')

    def test_empty_and_whitespace_return_empty(self):
        self.assertEqual(server.pattern_to_fd_regex(''), '')
        self.assertEqual(server.pattern_to_fd_regex('   '), '')


class TestDiscoverVtts(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.made = []
        for rel in [
            os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'),
            os.path.join('48k', 'idioti', '20260601', 'by10m', 'b.vtt'),
            os.path.join('48k', 'kontakt', '20260515', 'by10m', 'c.vtt'),
        ]:
            p = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w', encoding='utf-8') as f:
                f.write('WEBVTT\n')
            self.made.append(rel)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_empty_pattern_lists_all(self):
        found = server.discover_vtts(self.root, '')
        self.assertEqual(sorted(found), sorted(self.made))

    def test_pattern_filters(self):
        found = server.discover_vtts(self.root, 'idioti 202606')
        self.assertEqual(sorted(found),
                         sorted(m for m in self.made if 'idioti' in m))

    def test_limit_caps_count(self):
        found = server.discover_vtts(self.root, 'idioti', limit=1)
        self.assertEqual(len(found), 1)


class TestReindexWrite(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.dbfile = os.path.join(tempfile.mkdtemp(), 'lines.db')
        self._saved_args = server.args
        self._saved_db = server.db_duckdb
        server.args = SimpleNamespace(db=self.dbfile, root=self.root)
        server.init_database()

    def tearDown(self):
        try:
            server.db_duckdb.close()
        except Exception:
            pass
        server.args = self._saved_args
        server.db_duckdb = self._saved_db
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(os.path.dirname(self.dbfile), ignore_errors=True)

    def make_vtt(self, rel, lines):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        body = "WEBVTT\n\n"
        t = 0
        for text in lines:
            body += f"00:00:0{t}.000 --> 00:00:0{t+1}.000\n{text}\n\n"
            t += 1
        with open(p, 'w', encoding='utf-8') as f:
            f.write(body)
        return rel

    def line_count(self):
        return server.db_duckdb.execute("SELECT COUNT(*) FROM lines").fetchone()[0]

    def scopes_for(self, like):
        return server.db_duckdb.execute(
            "SELECT scope FROM scopes WHERE scope LIKE ?", (like,)).fetchall()

    def test_pattern_reindex_is_idempotent(self):
        self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'),
                      ['one', 'two', 'three'])
        n1 = server.reindex(self.root, 'idioti')
        c1 = self.line_count()
        n2 = server.reindex(self.root, 'idioti')
        c2 = self.line_count()
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)
        self.assertEqual(c1, 3)
        self.assertEqual(c2, 3)

    def test_pattern_reindex_picks_up_edits(self):
        rel = self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'),
                            ['one', 'two'])
        server.reindex(self.root, 'idioti')
        self.assertEqual(self.line_count(), 2)
        self.make_vtt(rel, ['one', 'two', 'three', 'four'])
        server.reindex(self.root, 'idioti')
        self.assertEqual(self.line_count(), 4)

    def test_pattern_reindex_does_not_touch_unmatched(self):
        server.db_duckdb.execute(
            "INSERT INTO lines VALUES ('48k/other/20200101/by10m/z.vtt','00:00:00.000','00:00:01.000','keep')")
        server.db_duckdb.execute("INSERT INTO scopes VALUES ('48k/other/20200101/by10m/z.vtt')")
        self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'), ['x'])
        server.reindex(self.root, 'idioti')
        kept = server.db_duckdb.execute(
            "SELECT COUNT(*) FROM lines WHERE filename = '48k/other/20200101/by10m/z.vtt'").fetchone()[0]
        self.assertEqual(kept, 1)
        self.assertEqual(len(self.scopes_for('%idioti%')), 1)

    def test_full_rebuild_clears_stale_rows(self):
        server.db_duckdb.execute(
            "INSERT INTO lines VALUES ('48k/gone/20200101/by10m/z.vtt','00:00:00.000','00:00:01.000','stale')")
        server.db_duckdb.execute("INSERT INTO scopes VALUES ('48k/gone/20200101/by10m/z.vtt')")
        self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'), ['x'])
        server.reindex(self.root, '')
        gone = server.db_duckdb.execute(
            "SELECT COUNT(*) FROM lines WHERE filename = '48k/gone/20200101/by10m/z.vtt'").fetchone()[0]
        self.assertEqual(gone, 0)
        self.assertEqual(self.line_count(), 1)


class TestReindexEndpoint(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.dbfile = os.path.join(tempfile.mkdtemp(), 'lines.db')
        self._saved_args = server.args
        self._saved_db = server.db_duckdb
        server.args = SimpleNamespace(db=self.dbfile, root=self.root)
        server.init_database()
        with server._reindex_lock:
            server._reindex_running = False

    def tearDown(self):
        for _ in range(100):
            with server._reindex_lock:
                running = server._reindex_running
            if not running:
                break
            time.sleep(0.05)
        try:
            server.db_duckdb.close()
        except Exception:
            pass
        server.args = self._saved_args
        server.db_duckdb = self._saved_db
        with server._reindex_lock:
            server._reindex_running = False
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(os.path.dirname(self.dbfile), ignore_errors=True)

    def make_vtt(self, rel):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nx\n")
        return rel

    def test_empty_pattern_rejected(self):
        res = server.start_reindex(self.root, '   ', server.REINDEX_LIMIT)
        self.assertEqual(res['status'], 'no pattern')
        self.assertEqual(res['matched'], 0)

    def test_nothing_matched(self):
        res = server.start_reindex(self.root, 'doesnotexist', server.REINDEX_LIMIT)
        self.assertEqual(res['status'], 'nothing matched')
        self.assertEqual(res['matched'], 0)

    def test_started_reports_matched_and_runs(self):
        self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'))
        res = server.start_reindex(self.root, 'idioti', server.REINDEX_LIMIT)
        self.assertEqual(res['status'], 'started')
        self.assertEqual(res['matched'], 1)
        for _ in range(50):
            with server._reindex_lock:
                running = server._reindex_running
            if not running:
                break
            time.sleep(0.1)
        n = server.db_duckdb.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
        self.assertEqual(n, 1)

    def test_truncated_flag_when_capped(self):
        for i in range(3):
            self.make_vtt(os.path.join('48k', 'idioti', '2026060%d' % i, 'by10m', 'a.vtt'))
        res = server.start_reindex(self.root, 'idioti', 2)
        self.assertEqual(res['matched'], 2)
        self.assertTrue(res['truncated'])

    def test_already_running_guard(self):
        self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'))
        with server._reindex_lock:
            server._reindex_running = True
        res = server.start_reindex(self.root, 'idioti', server.REINDEX_LIMIT)
        self.assertEqual(res['status'], 'already running')

    def test_reindex_uses_provided_files_over_discovery(self):
        rel = self.make_vtt(os.path.join('48k', 'idioti', '20260601', 'by10m', 'a.vtt'))
        n = server.reindex(self.root, 'pattern-that-would-not-match-xyz', None, files=[rel])
        self.assertEqual(n, 1)
        cnt = server.db_duckdb.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
        self.assertEqual(cnt, 1)


if __name__ == '__main__':
    unittest.main()