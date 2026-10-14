import sqlite3
import unittest
from session_search.charts import bar, history, estimated_time
from session_search.search_history import initialize, record, stats, sanitize

class MetricsTests(unittest.TestCase):
    def test_axes_missing_zero_and_time(self):
        self.assertEqual(bar(None), 'N/A')
        self.assertEqual(bar(0), '['+'░'*16+']')
        self.assertEqual(bar(100), '['+'█'*16+']')
        chart = history([(0,100),(100,0),(110,50)], 120)
        self.assertIn('−120s', chart)
        self.assertIn('−60s', chart)
        self.assertIn('now', chart)
        self.assertIn('•', chart)
        self.assertNotIn('█', history([(0,100)], 121))
    def test_speed_not_clamped_and_low_cpu_scaled(self):
        self.assertIn('200', history([(119, 150)], 120, unit=''))
        self.assertIn('10%', history([(119, 9)], 120))
        self.assertNotIn('100%', history([(119, 9)], 120))
        self.assertNotIn('█', history([(119, None)], 120))

    def test_eta_needs_complete_window_and_distinguishes_zero(self):
        self.assertEqual(estimated_time(100, 20, 30), 'Calculating…')
        self.assertEqual(estimated_time(100, 20, 60), '~5m')
        self.assertEqual(estimated_time(100, 0, 60), 'Calculating…')
        self.assertEqual(estimated_time(100, 20, 60, active=False), 'N/A')
        self.assertEqual(estimated_time(0, 0, 60), '0m')

    def test_facts_unknown_error_empty_and_redaction(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        initialize(db)
        for key in ('ok','error','empty','unknown'):
            record(db, dict(phase='start', id=key, started_at=100, parameters={'query':'Найти sk-secret123'}))
        record(db, dict(phase='finish', id='ok', finished_at=103, duration_seconds=3, result_count=1,
                       results=[dict(session_id='s1',chunk_id='c1',distance=.2)]))
        record(db, dict(phase='finish', id='empty', finished_at=105, duration_seconds=5, result_count=0))
        record(db, dict(phase='finish', id='error', finished_at=104, duration_seconds=4, error='token=hidden'))
        result=stats(db, 110)
        self.assertEqual((result['calls'], result['errors'], result['empty'], result['unknown'], result['average_seconds']), (4,1,1,1,4))
        self.assertEqual(db.execute('SELECT session_id,chunk_id,distance FROM search_results').fetchone(), ('s1','c1',.2))
        self.assertNotIn('secret123', db.execute('SELECT parameters FROM search_requests LIMIT 1').fetchone()[0])
        self.assertNotIn('hidden',db.execute("SELECT error FROM search_requests WHERE id='error'").fetchone()[0])
        self.assertEqual(stats(db, 90000)['calls'],0)
        self.assertEqual(sanitize({'api_key':'foo'}), {'api_key':'[REDACTED]'})
