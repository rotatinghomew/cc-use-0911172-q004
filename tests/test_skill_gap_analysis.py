import json
import tempfile
import unittest
from pathlib import Path
from job_match_market.analytics.skill_gap import (
    SkillGapAnalyzer,
    SkillGapError,
    analyze_skill_gaps,
    load_database_input,
)
from job_match_market.cli import main as cli_main
from job_match_market.storage import Database

CANDIDATES = [
    {'id': 'c1', 'skills': ['python'], 'minimum_salary': '7000', 'maximum_commute': 30,
     'work_modes': ['onsite'], 'employer_score': '5'},
    {'id': 'c2', 'skills': ['python', 'sql'], 'minimum_salary': '9000', 'maximum_commute': 20,
     'work_modes': ['onsite'], 'employer_score': '3'},
    {'id': 'c3', 'skills': ['python', 'sql', 'etl'], 'minimum_salary': '12000', 'maximum_commute': 60,
     'work_modes': ['onsite'], 'employer_score': '8'},
]
VACANCIES = [
    {'id': 'v1', 'required_skills': ['python', 'sql'], 'salary': '9000', 'commute_minutes': 20,
     'work_mode': 'onsite', 'industry': 'software', 'capacity': 1},
    {'id': 'v2', 'required_skills': ['python', 'sql'], 'salary': '8000', 'commute_minutes': 25,
     'work_mode': 'onsite', 'industry': 'software', 'capacity': 2},
    {'id': 'v3', 'required_skills': ['python', 'sql'], 'salary': '15000', 'commute_minutes': 40,
     'work_mode': 'onsite', 'industry': 'finance', 'capacity': 1},
]


class SkillGapTests(unittest.TestCase):
    def setUp(self):
        self.report = analyze_skill_gaps(CANDIDATES, VACANCIES)

    def pair(self, candidate_id, vacancy_id):
        return next(
            item for item in self.report['pairs']
            if item['candidate_id'] == candidate_id and item['vacancy_id'] == vacancy_id
        )

    def test_classifies_skill_only_block(self):
        self.assertEqual(self.pair('c1', 'v1')['categories'], ['skill'])
        self.assertTrue(self.pair('c1', 'v1')['skill_only'])
        self.assertEqual(self.pair('c1', 'v1')['missing_skills'], ['sql'])

    def test_classifies_salary_block(self):
        # c3 expects 12000, v1 pays 9000.
        self.assertIn('salary', self.pair('c3', 'v1')['categories'])
        self.assertEqual(self.pair('c3', 'v1')['missing_skills'], [])

    def test_classifies_commute_block(self):
        # c2 accepts 20 minutes, v3 is 40 minutes away.
        self.assertEqual(self.pair('c2', 'v3')['categories'], ['commute'])
        self.assertEqual(self.pair('c2', 'v3')['missing_skills'], [])

    def test_mixed_constraints_separated(self):
        # c1 lacks sql (skill) and v3 pays above c1's floor but is far away?
        # c3-v3: c3 has all skills but v3 salary is fine; commute 40 <= 60.
        blocked = [item for item in self.report['pairs'] if not item['feasible']]
        mixed = [item for item in blocked if len(item['categories']) > 1]
        for item in mixed:
            self.assertTrue(item['skill_only'] is False)
            self.assertTrue(item['other_blockers'])

    def test_scenario_reports_newly_feasible_then_capacity(self):
        scenario = self.report['skills']['sql']['scenario']
        # c1 becomes feasible for both v1 and v2 once sql is added...
        self.assertEqual(scenario['newly_feasible_pair_count'], 2)
        self.assertEqual(scenario['newly_feasible_candidates'], ['c1'])
        # ...but capacity/queueing yields exactly one distinct placement.
        self.assertEqual(scenario['additional_placements'], 1)
        self.assertEqual(len(scenario['newly_placed_candidates']), 1)
        self.assertEqual(
            scenario['newly_feasible_pair_count'],
            len(scenario['newly_feasible_pair_evidence']),
        )

    def test_scenario_reports_still_blocked_by_other_hard_constraint(self):
        candidates = [
            {'id': 'c1', 'skills': ['python'], 'minimum_salary': '20000',
             'maximum_commute': 30, 'work_modes': ['onsite'], 'employer_score': '1'},
        ]
        vacancies = [
            {'id': 'v1', 'required_skills': ['python', 'sql'], 'salary': '9000',
             'commute_minutes': 10, 'work_mode': 'onsite', 'industry': 'software', 'capacity': 1},
        ]
        report = analyze_skill_gaps(candidates, vacancies)
        scenario = report['skills']['sql']['scenario']
        self.assertEqual(scenario['newly_feasible_pair_count'], 0)
        self.assertEqual(scenario['still_blocked_pair_count'], 1)
        self.assertIn('salary_below_minimum', scenario['still_blocked_by_reason'])
        self.assertEqual(scenario['newly_placed_count'], 0)

    def test_scenario_does_not_claim_multi_skill_gap_as_solvable(self):
        candidates = [
            {'id': 'c1', 'skills': [], 'minimum_salary': '0', 'maximum_commute': 30,
             'work_modes': ['onsite'], 'employer_score': '1'},
        ]
        vacancies = [
            {'id': 'v1', 'required_skills': ['python', 'sql'], 'salary': '9000',
             'commute_minutes': 10, 'work_mode': 'onsite', 'industry': 'software', 'capacity': 1},
        ]
        report = analyze_skill_gaps(candidates, vacancies)
        scenario = report['skills']['python']['scenario']
        self.assertEqual(scenario['newly_feasible_pair_count'], 0)
        self.assertEqual(scenario['needs_more_skills_pair_count'], 1)
        self.assertEqual(scenario['trained_candidate_count'], 0)

    def test_ranking_uses_placements_not_vacancy_count(self):
        ranking = self.report['skill_ranking']
        self.assertEqual(ranking[0]['skill'], 'sql')
        self.assertEqual(ranking[0]['additional_placements'], 1)
        self.assertEqual(ranking[0]['rank'], 1)
        # python is required by the same vacancies but unlocks nothing.
        python = next(item for item in ranking if item['skill'] == 'python')
        self.assertEqual(python['additional_placements'], 0)
        self.assertGreater(python['rank'], 1)

    def test_one_placement_per_candidate(self):
        # Each person appears at most once in the placements and in every
        # scenario, even when several vacancies are feasible.
        candidate_ids = [item['candidate_id'] for item in self.report['placements']]
        self.assertEqual(candidate_ids, sorted(set(candidate_ids)))
        for skill, item in self.report['skills'].items():
            placements = item['scenario']['scenario_placements']
            self.assertEqual(len(placements), len(set(placements)))
            self.assertLessEqual(len(placements), len(set(placements.values())))

    def test_capacity_exhausted_is_not_called_employment(self):
        candidates = [
            {'id': 'c1', 'skills': ['python'], 'minimum_salary': '0', 'maximum_commute': 30,
             'work_modes': ['onsite'], 'employer_score': '5'},
            {'id': 'c2', 'skills': ['python'], 'minimum_salary': '0', 'maximum_commute': 30,
             'work_modes': ['onsite'], 'employer_score': '1'},
        ]
        vacancies = [
            {'id': 'v1', 'required_skills': ['python'], 'salary': '9000', 'commute_minutes': 10,
             'work_mode': 'onsite', 'industry': 'software', 'capacity': 1},
        ]
        report = analyze_skill_gaps(candidates, vacancies)
        self.assertEqual(report['summary']['baseline_placement_count'], 1)
        self.assertEqual(report['summary']['capacity_exhausted_candidates'], ['c2'])
        statuses = {row['candidate_id']: row['status'] for row in report['candidates']}
        self.assertEqual(statuses['c1'], 'placed')
        self.assertEqual(statuses['c2'], 'capacity_exhausted')

    def test_industry_and_skill_summary_traceable(self):
        cell = self.report['industry_skills']['software']['sql']
        self.assertEqual(cell['vacancy_ids'], ['v1', 'v2'])
        self.assertEqual(cell['candidate_ids_missing'], ['c1'])
        self.assertIn(['c1', 'v1'], cell['blocked_pairs'])
        self.assertEqual(cell['scenario_placed_candidate_ids_in_industry'], ['c1'])
        industry = self.report['industries']['software']
        self.assertEqual(industry['vacancy_ids'], ['v1', 'v2'])
        self.assertEqual(industry['placed_candidate_ids'], ['c2'])
        skill = self.report['skills']['sql']
        self.assertEqual(skill['vacancy_ids'], ['v1', 'v2', 'v3'])
        self.assertIn({'candidate_id': 'c1', 'vacancy_id': 'v1'}, skill['blocked_pair_evidence'])

    def test_skill_names_normalised(self):
        candidates = [{'id': 'c1', 'skills': [' Python '], 'minimum_salary': '0',
                       'maximum_commute': 30, 'work_modes': ['onsite'], 'employer_score': '1'}]
        vacancies = [{'id': 'v1', 'required_skills': ['PYTHON'], 'salary': '1',
                      'commute_minutes': 1, 'work_mode': 'onsite', 'industry': 'x', 'capacity': 1}]
        report = analyze_skill_gaps(candidates, vacancies)
        self.assertEqual(report['summary']['feasible_pair_count'], 1)
        self.assertIn('python', report['skills'])

    def test_zero_capacity_reported_honestly(self):
        candidates = [{'id': 'c1', 'skills': ['python'], 'minimum_salary': '0',
                       'maximum_commute': 30, 'work_modes': ['onsite'], 'employer_score': '1'}]
        vacancies = [{'id': 'v1', 'required_skills': ['python'], 'salary': '1',
                      'commute_minutes': 1, 'work_mode': 'onsite', 'industry': 'x', 'capacity': 0}]
        report = analyze_skill_gaps(candidates, vacancies)
        self.assertEqual(report['summary']['feasible_pair_count'], 1)
        self.assertEqual(report['summary']['baseline_placement_count'], 0)

    def test_work_mode_mismatch_is_not_skill(self):
        candidates = [{'id': 'c1', 'skills': ['python'], 'minimum_salary': '0',
                       'maximum_commute': 30, 'work_modes': ['remote'], 'employer_score': '1'}]
        vacancies = [{'id': 'v1', 'required_skills': ['python'], 'salary': '1',
                      'commute_minutes': 1, 'work_mode': 'onsite', 'industry': 'x', 'capacity': 1}]
        report = analyze_skill_gaps(candidates, vacancies)
        pair = report['pairs'][0]
        self.assertEqual(pair['categories'], ['work_mode'])
        self.assertEqual(pair['missing_skills'], [])

    def test_empty_input(self):
        report = analyze_skill_gaps([], [])
        self.assertEqual(report['summary']['pair_count'], 0)
        self.assertEqual(report['summary']['baseline_placement_count'], 0)
        self.assertEqual(report['skill_ranking'], [])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(SkillGapError):
            analyze_skill_gaps([CANDIDATES[0], CANDIDATES[0]], [])

    def test_invalid_rows_rejected(self):
        with self.assertRaises(SkillGapError):
            analyze_skill_gaps([{'skills': ['python']}], [])

    def test_exposure_rows_are_read_not_written(self):
        exposures = [{'vacancy_id': 'v1', 'count': 4}]
        report = analyze_skill_gaps(CANDIDATES, VACANCIES, exposures)
        self.assertEqual(report['method']['exposure_history'], 'baseline counts are read only and never modified by scenarios')
        self.assertTrue(report['read_only'])

    def test_database_input_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / 'db.sqlite3')
            with database.transaction() as connection:
                database.put_record(connection, 'candidate', {
                    'candidate_id': 'c1', 'owner_id': 'o1', 'category': 'youth',
                    'status': 'active', 'effective_from': '2026-11-16', 'quantity': 1,
                    'amount': '0', 'attributes': {
                        'skills': ['python'], 'minimum_salary': '0',
                        'maximum_commute': 30, 'work_modes': ['onsite'], 'employer_score': '1',
                    },
                })
                database.put_record(connection, 'vacancy', {
                    'vacancy_id': 'v1', 'owner_id': 'o1', 'category': 'software',
                    'status': 'active', 'effective_from': '2026-11-16', 'quantity': 2,
                    'amount': '0', 'attributes': {
                        'required_skills': ['python'], 'salary': '9000',
                        'commute_minutes': 10, 'work_mode': 'onsite', 'capacity': 2,
                    },
                })
                database.put_record(connection, 'exposure', {
                    'exposure_id': 'e1', 'owner_id': 'o1', 'category': 'software',
                    'status': 'active', 'effective_from': '2026-11-16', 'quantity': 3,
                    'amount': '0', 'attributes': {'vacancy_id': 'v1'},
                })
            before = database.counts()
            with database.transaction() as connection:
                before_events = connection.execute('SELECT COUNT(*) FROM events').fetchone()[0]
                before_outbox = connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
                before_exposures = [dict(row) for row in connection.execute(
                    'SELECT record_id,quantity,attributes_json,version FROM records WHERE kind=? ORDER BY record_id',
                    ('exposure',),
                )]
            candidates, vacancies, exposures = load_database_input(database)
            report = analyze_skill_gaps(candidates, vacancies, exposures)
            self.assertEqual(database.counts(), before)
            with database.transaction() as connection:
                self.assertEqual(
                    connection.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                    before_events,
                )
                self.assertEqual(
                    connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],
                    before_outbox,
                )
                self.assertEqual(
                    [dict(row) for row in connection.execute(
                        'SELECT record_id,quantity,attributes_json,version FROM records WHERE kind=? ORDER BY record_id',
                        ('exposure',),
                    )],
                    before_exposures,
                )
            self.assertEqual(exposures, {'v1': 3})
            self.assertEqual(report['summary']['baseline_placement_count'], 1)
            self.assertEqual(database.list_records('exposure')[0]['quantity'], 3)

    def test_ranking_prefers_cheaper_skill_at_equal_net_gain(self):
        # Directly exercise the ranking policy with two skills that create the
        # same net employment but consume different amounts of quota.
        analyzer = SkillGapAnalyzer()
        rows = {
            'broad': {
                'vacancy_count': 1,
                'blocked_pair_count': 1,
                'industry_counts': {'x': 1},
                'scenario': {
                    'net_placement_change': 1, 'additional_placements': 1,
                    'newly_placed_count': 1, 'displaced_candidate_ids': [],
                    'newly_feasible_pair_count': 1, 'still_blocked_pair_count': 0,
                    'needs_more_skills_pair_count': 0,
                    'newly_feasible_but_unplaced_candidates': [],
                    'trained_candidate_count': 4,
                },
            },
            'cheap': {
                'vacancy_count': 3,
                'blocked_pair_count': 3,
                'industry_counts': {'y': 3},
                'scenario': {
                    'net_placement_change': 1, 'additional_placements': 1,
                    'newly_placed_count': 1, 'displaced_candidate_ids': [],
                    'newly_feasible_pair_count': 1, 'still_blocked_pair_count': 0,
                    'needs_more_skills_pair_count': 0,
                    'newly_feasible_but_unplaced_candidates': [],
                    'trained_candidate_count': 1,
                },
            },
        }
        ranking = analyzer._ranking(rows)
        self.assertEqual([item['skill'] for item in ranking], ['cheap', 'broad'])
        self.assertEqual(ranking[0]['net_placements_per_trained_candidate'], 1.0)
        self.assertEqual(ranking[1]['net_placements_per_trained_candidate'], 0.25)

    def test_ranking_keeps_vacancy_count_as_tiebreak_only(self):
        analyzer = SkillGapAnalyzer()
        def row(net, additional, trained, vacancies):
            return {
                'vacancy_count': vacancies,
                'blocked_pair_count': vacancies,
                'industry_counts': {},
                'scenario': {
                    'net_placement_change': net, 'additional_placements': additional,
                    'newly_placed_count': additional, 'displaced_candidate_ids': [],
                    'newly_feasible_pair_count': 0, 'still_blocked_pair_count': 0,
                    'needs_more_skills_pair_count': 0,
                    'newly_feasible_but_unplaced_candidates': [],
                    'trained_candidate_count': trained,
                },
            }
        ranking = analyzer._ranking({
            'many_vacancies_no_gain': row(0, 0, 0, 100),
            'one_gain': row(1, 1, 1, 1),
        })
        self.assertEqual([item['skill'] for item in ranking], ['one_gain', 'many_vacancies_no_gain'])

    def test_pair_evidence_can_be_suppressed(self):
        report = analyze_skill_gaps(CANDIDATES, VACANCIES, include_pair_evidence=False)
        self.assertEqual(report['pairs'], [])
        # Aggregate sections remain available and traceable.
        self.assertIn('industry_skills', report)
        self.assertTrue(report['skills'])

    def test_reserved_slots_reduce_available_capacity(self):
        report = analyze_skill_gaps(CANDIDATES, VACANCIES, reserved={'v1': 1, 'v2': 1})
        self.assertEqual(report['summary']['reserved_slots'], 2)
        self.assertEqual(report['summary']['available_capacity'], 2)
        # v1 now has no slots, so its placements disappear without touching
        # the vacancy record itself.
        self.assertEqual(report['summary']['baseline_placement_count'], 1)
        self.assertEqual(report['summary']['available_capacity'], 2)

    def test_report_mapping_interface(self):
        self.assertEqual(self.report['summary']['candidate_count'], 3)
        self.assertIsInstance(self.report.get('skills'), dict)
        self.assertIn('skill_ranking', self.report.keys())

    def test_analyzer_reusable_and_deterministic(self):
        analyzer = SkillGapAnalyzer()
        first = analyzer.analyze(CANDIDATES, VACANCIES).as_dict()
        second = analyzer.analyze(list(reversed(CANDIDATES)), list(reversed(VACANCIES))).as_dict()
        self.assertEqual(first['skill_ranking'], second['skill_ranking'])
        self.assertEqual(first['summary'], second['summary'])

    def test_reporter_skill_gap_is_read_only(self):
        from job_match_market.reporting import Reporter
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / 'db.sqlite3')
            with database.transaction() as connection:
                database.put_record(connection, 'candidate', {
                    'candidate_id': 'c1', 'owner_id': 'o1', 'category': 'youth',
                    'status': 'active', 'effective_from': '2026-11-16', 'quantity': 1,
                    'amount': '0', 'attributes': {
                        'skills': ['python'], 'minimum_salary': '0',
                        'maximum_commute': 30, 'work_modes': ['onsite'],
                    },
                })
                database.put_record(connection, 'vacancy', {
                    'vacancy_id': 'v1', 'owner_id': 'o1', 'category': 'software',
                    'status': 'active', 'effective_from': '2026-11-16', 'quantity': 1,
                    'amount': '0', 'attributes': {
                        'required_skills': ['python', 'sql'], 'salary': '9000',
                        'commute_minutes': 10, 'work_mode': 'onsite', 'capacity': 1,
                    },
                })
            report = Reporter(database).skill_gap()
            self.assertEqual(report['summary']['candidate_count'], 1)
            self.assertEqual(report['skill_ranking'][0]['skill'], 'sql')
            self.assertEqual(database.counts(), {'candidate': 1, 'vacancy': 1})

    def test_cli_fixture_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / 'db.sqlite3'
            output_path = Path(tmp) / 'gap.json'
            fixture_path = Path(__file__).resolve().parent.parent / 'fixtures' / 'skill_gap.json'
            code = cli_main([
                '--database', str(database_path), 'skill-gap',
                '--fixture', str(fixture_path), '--output', str(output_path),
            ])
            self.assertEqual(code, 0)
            payload = json.loads(output_path.read_text(encoding='utf-8'))
            self.assertTrue(payload['read_only'])
            self.assertEqual(payload['summary']['candidate_count'], 5)
            self.assertEqual(len(payload['skill_ranking']), 5)
            self.assertEqual(Database(database_path).counts(), {})

    def test_data_quality_warns_when_only_candidates_exist(self):
        report = analyze_skill_gaps(CANDIDATES, [])
        self.assertTrue(any(
            warning.startswith('no_vacancy_records')
            for warning in report['summary']['data_quality']['warnings']
        ))
        self.assertEqual(report['summary']['baseline_placement_count'], 0)

    def test_data_quality_flags_candidate_without_skills(self):
        report = analyze_skill_gaps(
            [{'id': 'c1', 'skills': [], 'minimum_salary': '0', 'maximum_commute': 30,
              'work_modes': ['onsite'], 'employer_score': '1'}],
            VACANCIES,
        )
        self.assertEqual(
            report['summary']['data_quality']['candidate_ids_without_skills'], ['c1']
        )
        self.assertIn(
            'candidates_without_skill_data',
            report['summary']['data_quality']['warnings'],
        )


if __name__ == '__main__':
    unittest.main()
