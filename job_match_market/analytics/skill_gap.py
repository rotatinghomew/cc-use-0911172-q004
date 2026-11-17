"""Read-only skill-gap analysis for the job match market.

The analysis deliberately shares its feasibility and allocation rules with the
live matching path (:class:`job_match_market.market_clearing.fair_match.FairMatchMarket`)
so that "required skills", "candidate skills" and "a realistic match" are all
measured with the same yardstick.

Three questions are answered:

* Which hard constraint blocks a candidate/vacancy pair: a missing skill, the
  salary floor, the commute limit, the work mode, or a mixture?
* After a skill is added, which pairs become feasible, which of those actually
  turn into placements once vacancy capacity is taken into account, and which
  remain blocked by salary/commute/work mode or by other missing skills?
* Which skill should training quota target?  Ranking is based on additional
  *distinct* placements under a capacity-aware clearing, not on vacancy counts.

Everything in this module is read-only.  It never writes to
:class:`job_match_market.storage.Database`, never consumes vacancy capacity in
the database and never touches exposure history.  Simulations are performed on
fresh in-memory copies of the input rows.
"""
from __future__ import annotations

import decimal
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..market_clearing.fair_match import FairMatchMarket


class SkillGapError(ValueError):
    """Raised when analysis input is malformed."""


# Reason codes returned by FairMatchMarket.feasible -> reporting category.
REASON_CATEGORIES = {
    'missing_skills': 'skill',
    'salary_below_minimum': 'salary',
    'commute_exceeds_limit': 'commute',
    'work_mode_mismatch': 'work_mode',
}
CATEGORY_REASONS = {category: reason for reason, category in REASON_CATEGORIES.items()}

# Stable output ordering.
CATEGORY_ORDER = ('skill', 'salary', 'commute', 'work_mode')


def _text(value: Any, name: str) -> str:
    if value is None:
        raise SkillGapError(f'{name} is required')
    result = str(value).strip()
    if not result:
        raise SkillGapError(f'{name} is required')
    return result


def _skill(value: Any) -> str:
    result = str(value).strip().lower()
    if not result:
        raise SkillGapError('skill names cannot be empty')
    return result


def _skill_list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    else:
        try:
            values = tuple(value)
        except TypeError as exc:
            raise SkillGapError(f'{name} must be a sequence of skill names') from exc
    result = sorted({_skill(item) for item in values})
    return tuple(result)


def _decimal(value: Any, name: str, default: str = '0') -> decimal.Decimal:
    try:
        result = decimal.Decimal(str(default if value is None else value))
    except (decimal.InvalidOperation, ValueError) as exc:
        raise SkillGapError(f'{name} must be decimal') from exc
    if not result.is_finite() or result < 0:
        raise SkillGapError(f'{name} cannot be negative')
    return result.quantize(decimal.Decimal('0.01'))


def _integer(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise SkillGapError(f'{name} must be an integer')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SkillGapError(f'{name} must be an integer') from exc
    if result < 0:
        raise SkillGapError(f'{name} cannot be negative')
    return result


def _pick(source: Mapping[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    """Read a value from the row, then from ``row['attributes']``."""
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    attributes = source.get('attributes')
    if isinstance(attributes, Mapping):
        for key in keys:
            if key in attributes and attributes[key] is not None:
                return attributes[key]
    return default


def _mapping(value: Any, name: str) -> dict[str, int]:
    """Normalise an exposure mapping or a list of exposure rows."""
    if value is None:
        return {}
    result: dict[str, int] = {}
    if isinstance(value, Mapping):
        rows: Iterable[Any] = value.items()
        for key, count in rows:
            vacancy_id = str(key)
            result[vacancy_id] = result.get(vacancy_id, 0) + _integer(count, f'{name}.{vacancy_id}', 0)
        return result
    if not isinstance(value, (list, tuple)):
        raise SkillGapError(f'{name} must be a mapping or a sequence of rows')
    for row in value:
        if not isinstance(row, Mapping):
            raise SkillGapError(f'{name} rows must be mappings')
        vacancy = row.get('vacancy_id') or row.get('id')
        if vacancy is None:
            raise SkillGapError(f'{name} row needs vacancy_id')
        count = row.get('count', row.get('quantity', 1))
        key = str(vacancy)
        result[key] = result.get(key, 0) + _integer(count, f'{name}.{key}', 0)
    return result


@dataclass(frozen=True)
class CandidateProfile:
    candidate_id: str
    skills: tuple[str, ...]
    minimum_salary: decimal.Decimal
    maximum_commute: int
    work_modes: tuple[str, ...] | None
    employer_score: decimal.Decimal
    owner_id: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def market_view(self, extra_skills: Iterable[str] = ()) -> dict[str, Any]:
        skills = tuple(sorted(set(self.skills) | {_skill(item) for item in extra_skills}))
        view: dict[str, Any] = {
            'id': self.candidate_id,
            'skills': list(skills),
            'minimum_salary': str(self.minimum_salary),
            'maximum_commute': self.maximum_commute,
            'employer_score': str(self.employer_score),
        }
        if self.work_modes is not None:
            view['work_modes'] = list(self.work_modes)
        return view

    def as_dict(self) -> dict[str, Any]:
        return {
            'candidate_id': self.candidate_id,
            'skills': list(self.skills),
            'minimum_salary': str(self.minimum_salary),
            'maximum_commute': self.maximum_commute,
            'work_modes': None if self.work_modes is None else list(self.work_modes),
            'employer_score': str(self.employer_score),
            'owner_id': self.owner_id,
        }


@dataclass(frozen=True)
class VacancyProfile:
    vacancy_id: str
    required_skills: tuple[str, ...]
    salary: decimal.Decimal
    commute_minutes: int
    work_mode: str
    industry: str
    capacity: int
    owner_id: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def market_view(self, reserved: int = 0) -> dict[str, Any]:
        available = max(0, self.capacity - reserved)
        return {
            'id': self.vacancy_id,
            'required_skills': list(self.required_skills),
            'salary': str(self.salary),
            'commute_minutes': self.commute_minutes,
            'work_mode': self.work_mode,
            'industry': self.industry,
            'capacity': available,
            'capacity_total': self.capacity,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            'vacancy_id': self.vacancy_id,
            'required_skills': list(self.required_skills),
            'salary': str(self.salary),
            'commute_minutes': self.commute_minutes,
            'work_mode': self.work_mode,
            'industry': self.industry,
            'capacity': self.capacity,
            'owner_id': self.owner_id,
        }


def candidate_profile(row: Mapping[str, Any]) -> CandidateProfile:
    if not isinstance(row, Mapping):
        raise SkillGapError('candidate rows must be mappings')
    identifier = _text(row.get('candidate_id') or row.get('id'), 'candidate_id')
    raw_work_modes = _pick(row, ('work_modes', 'work_mode'))
    if raw_work_modes is None:
        work_modes = None
    elif isinstance(raw_work_modes, str):
        work_modes = (_text(raw_work_modes, 'work_mode'),)
    else:
        work_modes = tuple(_text(item, 'work_mode') for item in raw_work_modes)
    return CandidateProfile(
        candidate_id=identifier,
        skills=_skill_list(_pick(row, ('skills', 'existing_skills'), ()), 'skills'),
        minimum_salary=_decimal(_pick(row, ('minimum_salary', 'salary_floor')), 'minimum_salary'),
        maximum_commute=_integer(_pick(row, ('maximum_commute', 'max_commute', 'commute_limit')), 'maximum_commute', 999),
        work_modes=work_modes,
        employer_score=_decimal(_pick(row, ('employer_score', 'score')), 'employer_score'),
        owner_id=row.get('owner_id'),
        attributes=dict(row.get('attributes') or {}),
    )


def vacancy_profile(row: Mapping[str, Any]) -> VacancyProfile:
    if not isinstance(row, Mapping):
        raise SkillGapError('vacancy rows must be mappings')
    identifier = _text(row.get('vacancy_id') or row.get('id'), 'vacancy_id')
    capacity = _pick(row, ('capacity', 'slots'))
    if capacity is None:
        # Fall back to the record quantity, and only to 1 when the row carries
        # no capacity signal at all.  A quantity of 0 means no slots, which is
        # reported truthfully rather than rounded up.
        capacity = 1 if 'quantity' not in row else row.get('quantity')
    industry = _pick(row, ('industry',))
    if industry is None:
        industry = row.get('category', 'other')
    return VacancyProfile(
        vacancy_id=identifier,
        required_skills=_skill_list(_pick(row, ('required_skills', 'skills'), ()), 'required_skills'),
        salary=_decimal(_pick(row, ('salary', 'monthly_salary')), 'salary'),
        commute_minutes=_integer(_pick(row, ('commute_minutes', 'commute')), 'commute_minutes', 0),
        work_mode=_text(_pick(row, ('work_mode',), 'onsite'), 'work_mode'),
        industry=_text(industry, 'industry'),
        capacity=_integer(capacity, 'capacity', 1),
        owner_id=row.get('owner_id'),
        attributes=dict(row.get('attributes') or {}),
    )


@dataclass(frozen=True)
class PairAssessment:
    candidate_id: str
    vacancy_id: str
    industry: str
    feasible: bool
    missing_skills: tuple[str, ...]
    blockers: tuple[str, ...]
    categories: tuple[str, ...]

    @property
    def skill_only(self) -> bool:
        return bool(self.missing_skills) and self.categories == ('skill',)

    @property
    def other_blockers(self) -> tuple[str, ...]:
        return tuple(reason for reason in self.blockers if reason != 'missing_skills')

    def as_dict(self) -> dict[str, Any]:
        return {
            'candidate_id': self.candidate_id,
            'vacancy_id': self.vacancy_id,
            'industry': self.industry,
            'feasible': self.feasible,
            'missing_skills': list(self.missing_skills),
            'blockers': list(self.blockers),
            'categories': list(self.categories),
            'skill_only': self.skill_only,
            'other_blockers': list(self.other_blockers),
        }


@dataclass(frozen=True)
class SkillGapReport:
    summary: Mapping[str, Any]
    candidates: tuple[Mapping[str, Any], ...]
    pairs: tuple[Mapping[str, Any], ...]
    industries: Mapping[str, Any]
    industry_skills: Mapping[str, Any]
    skills: Mapping[str, Any]
    skill_ranking: tuple[Mapping[str, Any], ...]
    placements: tuple[Mapping[str, Any], ...]
    method: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            'schema_version': 1,
            'read_only': True,
            'method': dict(self.method),
            'summary': dict(self.summary),
            'placements': [dict(item) for item in self.placements],
            'candidates': [dict(item) for item in self.candidates],
            'pairs': [dict(item) for item in self.pairs],
            'industries': {key: dict(value) for key, value in self.industries.items()},
            'industry_skills': {
                industry: {skill: dict(cell) for skill, cell in cells.items()}
                for industry, cells in self.industry_skills.items()
            },
            'skills': {key: dict(value) for key, value in self.skills.items()},
            'skill_ranking': [dict(item) for item in self.skill_ranking],
        }

    # Be forgiving for callers that expect a plain JSON-like mapping.
    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def keys(self):
        return self.as_dict().keys()


class SkillGapAnalyzer:
    """Compare skills, requirements and capacity-aware matching without writing."""

    def __init__(self, market: FairMatchMarket | None = None):
        self.market = market or FairMatchMarket()

    # ------------------------------------------------------------------ input
    def _profiles(
        self,
        candidates: Iterable[Mapping[str, Any]],
        vacancies: Iterable[Mapping[str, Any]],
    ) -> tuple[tuple[CandidateProfile, ...], tuple[VacancyProfile, ...]]:
        candidate_profiles: list[CandidateProfile] = []
        seen_candidates: set[str] = set()
        for row in candidates:
            profile = candidate_profile(row)
            if profile.candidate_id in seen_candidates:
                raise SkillGapError(f'duplicate candidate_id: {profile.candidate_id}')
            seen_candidates.add(profile.candidate_id)
            candidate_profiles.append(profile)
        vacancy_profiles: list[VacancyProfile] = []
        seen_vacancies: set[str] = set()
        for row in vacancies:
            profile = vacancy_profile(row)
            if profile.vacancy_id in seen_vacancies:
                raise SkillGapError(f'duplicate vacancy_id: {profile.vacancy_id}')
            seen_vacancies.add(profile.vacancy_id)
            vacancy_profiles.append(profile)
        candidate_profiles.sort(key=lambda item: item.candidate_id)
        vacancy_profiles.sort(key=lambda item: item.vacancy_id)
        return tuple(candidate_profiles), tuple(vacancy_profiles)

    def _pair(self, candidate: CandidateProfile, vacancy: VacancyProfile) -> PairAssessment:
        ok, reasons = self.market.feasible(candidate.market_view(), vacancy.market_view())
        missing = tuple(sorted(set(vacancy.required_skills) - set(candidate.skills)))
        blockers = tuple(sorted(set(reasons)))
        categories = tuple(
            category for category in CATEGORY_ORDER
            if any(REASON_CATEGORIES.get(reason) == category for reason in blockers)
        )
        return PairAssessment(
            candidate_id=candidate.candidate_id,
            vacancy_id=vacancy.vacancy_id,
            industry=vacancy.industry,
            feasible=ok,
            missing_skills=missing,
            blockers=blockers,
            categories=categories,
        )

    def _clear(
        self,
        candidate_views: Sequence[Mapping[str, Any]],
        vacancy_views: Sequence[Mapping[str, Any]],
        exposures: Mapping[str, int],
    ) -> dict[str, str]:
        # FairMatchMarket.clear is deterministic and returns at most one
        # vacancy per candidate, so one person can never be counted twice.
        return self.market.clear(
            [dict(view) for view in candidate_views],
            [dict(view) for view in vacancy_views],
            dict(exposures),
        )

    # ----------------------------------------------------------------- report
    def analyze(
        self,
        candidates: Iterable[Mapping[str, Any]],
        vacancies: Iterable[Mapping[str, Any]],
        exposures: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        include_pair_evidence: bool = True,
        reserved: Mapping[str, Any] | None = None,
    ) -> SkillGapReport:
        candidate_profiles, vacancy_profiles = self._profiles(candidates, vacancies)
        exposure_counts = _mapping(exposures, 'exposures')
        # ``reserved`` maps a vacancy_id to slots already committed outside this
        # analysis.  It is subtracted from capacity for every clearing so the
        # analysis does not pretend an already-occupied slot is available.  It
        # is a caller-supplied assumption, never read from or written to the
        # live matching tables.
        reserved_counts = _mapping(reserved, 'reserved')
        vacancy_views = [
            vacancy.market_view(reserved_counts.get(vacancy.vacancy_id, 0))
            for vacancy in vacancy_profiles
        ]
        pair_map: dict[tuple[str, str], PairAssessment] = {}
        for candidate in candidate_profiles:
            for vacancy in vacancy_profiles:
                pair_map[(candidate.candidate_id, vacancy.vacancy_id)] = self._pair(candidate, vacancy)

        baseline = self._clear(
            [candidate.market_view() for candidate in candidate_profiles],
            vacancy_views,
            exposure_counts,
        )

        candidate_rows = self._candidate_rows(candidate_profiles, vacancy_profiles, pair_map, baseline)
        industry_rows = self._industry_rows(vacancy_profiles, pair_map, baseline)
        skill_rows = self._skill_rows(
            candidate_profiles, vacancy_profiles, pair_map, baseline, exposure_counts, vacancy_views
        )
        industry_skill_rows = self._industry_skill_rows(
            vacancy_profiles, pair_map, baseline, skill_rows
        )
        ranking = self._ranking(skill_rows)
        summary = self._summary(
            candidate_profiles, vacancy_profiles, pair_map, candidate_rows, baseline, ranking
        )
        summary['reserved_slots'] = sum(
            min(reserved_counts.get(vacancy.vacancy_id, 0), vacancy.capacity)
            for vacancy in vacancy_profiles
        )
        summary['available_capacity'] = sum(
            max(0, vacancy.capacity - reserved_counts.get(vacancy.vacancy_id, 0))
            for vacancy in vacancy_profiles
        )
        summary['data_quality'] = self._data_quality(candidate_profiles, vacancy_profiles)
        placements = tuple(
            {
                'candidate_id': candidate_id,
                'vacancy_id': vacancy_id,
                'industry': next(
                    vacancy.industry for vacancy in vacancy_profiles
                    if vacancy.vacancy_id == vacancy_id
                ),
            }
            for candidate_id, vacancy_id in sorted(baseline.items())
        )
        pairs = tuple(
            assessment.as_dict() for assessment in pair_map.values()
        ) if include_pair_evidence else ()
        method = {
            'feasibility': 'FairMatchMarket.feasible',
            'allocation': 'FairMatchMarket.clear',
            'capacity_aware': True,
            'one_vacancy_per_candidate': True,
            'placement_counting': 'placements are distinct candidate_id values; pair counts are never summed as employment',
            'read_only': True,
            'database_writes': 'none',
            'exposure_history': 'baseline counts are read only and never modified by scenarios',
            'skill_normalisation': 'trimmed and lower-cased before comparison',
            'blocker_categories': {
                'skill': 'missing_skills',
                'salary': 'salary_below_minimum',
                'commute': 'commute_exceeds_limit',
                'work_mode': 'work_mode_mismatch',
                'capacity': 'allocation-level, reported separately from pair feasibility',
            },
            'scenario_assumption': (
                'skill S is added to every candidate for whom S is the only missing skill '
                'of at least one vacancy, then the full capacity-aware clearing is rerun'
            ),
            'capacity': 'vacancy capacity minus caller-supplied reserved slots; never written back',
            'reserved_slots': 'optional caller assumption, not read from live matching tables',
            'baseline': (
                'baseline placements are a fresh simulated clearing of current data, '
                'not a replay of any historical matching run'
            ),
            'ranking': 'net distinct placements per skill under capacity-aware clearing; vacancy counts are tie-breakers only',
        }
        return SkillGapReport(
            summary=summary,
            candidates=tuple(candidate_rows),
            pairs=pairs,
            industries=industry_rows,
            industry_skills=industry_skill_rows,
            skills=skill_rows,
            skill_ranking=tuple(ranking),
            placements=placements,
            method=method,
        )

    # --------------------------------------------------------------- sections
    def _data_quality(
        self,
        candidates: Sequence[CandidateProfile],
        vacancies: Sequence[VacancyProfile],
    ) -> dict[str, Any]:
        """Surface missing inputs instead of silently reporting zero gaps."""
        without_skills = sorted(
            candidate.candidate_id for candidate in candidates if not candidate.skills
        )
        without_requirements = sorted(
            vacancy.vacancy_id for vacancy in vacancies if not vacancy.required_skills
        )
        warnings: list[str] = []
        if candidates and not vacancies:
            warnings.append('no_vacancy_records: analysis cannot rank skills without vacancies')
        if vacancies and without_requirements:
            warnings.append('vacancies_without_required_skills')
        if candidates and without_skills:
            warnings.append('candidates_without_skill_data')
        return {
            'candidates_without_skills': len(without_skills),
            'candidate_ids_without_skills': without_skills,
            'vacancies_without_required_skills': len(without_requirements),
            'vacancy_ids_without_required_skills': without_requirements,
            'warnings': warnings,
        }

    def _candidate_rows(
        self,
        candidates: Sequence[CandidateProfile],
        vacancies: Sequence[VacancyProfile],
        pairs: Mapping[tuple[str, str], PairAssessment],
        placements: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            assessments = [pairs[(candidate.candidate_id, vacancy.vacancy_id)] for vacancy in vacancies]
            feasible = [item for item in assessments if item.feasible]
            blocked = [item for item in assessments if not item.feasible]
            categories = sorted({category for item in blocked for category in item.categories}, key=CATEGORY_ORDER.index)
            blockers_by_reason: dict[str, int] = {}
            for item in blocked:
                for reason in item.blockers:
                    blockers_by_reason[reason] = blockers_by_reason.get(reason, 0) + 1
            missing: set[str] = set()
            for item in blocked:
                missing.update(item.missing_skills)
            placement = placements.get(candidate.candidate_id)
            if placement is not None:
                status = 'placed'
            elif feasible:
                status = 'capacity_exhausted'
            elif not assessments:
                status = 'no_vacancies'
            elif categories == ['skill']:
                status = 'skill_gap'
            elif categories == ['salary']:
                status = 'salary_gap'
            elif categories == ['commute']:
                status = 'commute_gap'
            elif categories == ['work_mode']:
                status = 'work_mode_gap'
            else:
                status = 'mixed_constraints'
            rows.append({
                'candidate_id': candidate.candidate_id,
                'status': status,
                'placement_vacancy_id': placement,
                'feasible_pair_count': len(feasible),
                'blocked_pair_count': len(blocked),
                'feasible_vacancy_ids': sorted(item.vacancy_id for item in feasible),
                'blocked_vacancy_ids': sorted(item.vacancy_id for item in blocked),
                'blocked_categories': categories,
                'blockers_by_reason': dict(sorted(blockers_by_reason.items())),
                'missing_skills': sorted(missing),
            })
        return rows

    def _industry_rows(
        self,
        vacancies: Sequence[VacancyProfile],
        pairs: Mapping[tuple[str, str], PairAssessment],
        placements: Mapping[str, str],
    ) -> dict[str, Any]:
        groups: dict[str, list[VacancyProfile]] = {}
        for vacancy in vacancies:
            groups.setdefault(vacancy.industry, []).append(vacancy)
        result: dict[str, Any] = {}
        for industry in sorted(groups):
            members = groups[industry]
            member_ids = {item.vacancy_id for item in members}
            assessments = [item for item in pairs.values() if item.vacancy_id in member_ids]
            blocked = [item for item in assessments if not item.feasible]
            blockers_by_reason: dict[str, int] = {}
            for item in blocked:
                for reason in item.blockers:
                    blockers_by_reason[reason] = blockers_by_reason.get(reason, 0) + 1
            placed = sorted(
                candidate_id for candidate_id, vacancy_id in placements.items()
                if vacancy_id in member_ids
            )
            skill_demand: dict[str, int] = {}
            for vacancy in members:
                for skill in vacancy.required_skills:
                    skill_demand[skill] = skill_demand.get(skill, 0) + 1
            result[industry] = {
                'industry': industry,
                'vacancy_count': len(members),
                'vacancy_ids': sorted(member_ids),
                'candidate_count': len({item.candidate_id for item in assessments}),
                'candidate_ids': sorted({item.candidate_id for item in assessments}),
                'pair_count': len(assessments),
                'feasible_pair_count': len(assessments) - len(blocked),
                'blocked_pair_count': len(blocked),
                'blocked_pairs_by_reason': dict(sorted(blockers_by_reason.items())),
                'placement_count': len(placed),
                'placed_candidate_ids': placed,
                'skill_vacancy_counts': dict(sorted(skill_demand.items())),
            }
        return result

    def _industry_skill_rows(
        self,
        vacancies: Sequence[VacancyProfile],
        pairs: Mapping[tuple[str, str], PairAssessment],
        placements: Mapping[str, str],
        skill_rows: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Industry x skill cells that can be traced back to candidates/vacancies."""
        vacancy_by_id = {vacancy.vacancy_id: vacancy for vacancy in vacancies}
        cells: dict[str, dict[str, dict[str, Any]]] = {}
        for assessment in pairs.values():
            vacancy = vacancy_by_id[assessment.vacancy_id]
            for skill in vacancy.required_skills:
                cell = cells.setdefault(vacancy.industry, {}).setdefault(skill, {
                    'vacancy_ids': set(),
                    'missing_candidate_ids': set(),
                    'skill_only_pairs': [],
                    'blocked_pairs': [],
                    'mixed_pairs': [],
                })
                cell['vacancy_ids'].add(vacancy.vacancy_id)
                if skill in assessment.missing_skills:
                    cell['missing_candidate_ids'].add(assessment.candidate_id)
                    cell['blocked_pairs'].append([assessment.candidate_id, assessment.vacancy_id])
                    if set(assessment.missing_skills) == {skill}:
                        cell['skill_only_pairs'].append([assessment.candidate_id, assessment.vacancy_id])
                    else:
                        cell['mixed_pairs'].append([assessment.candidate_id, assessment.vacancy_id])
        result: dict[str, Any] = {}
        for industry in sorted(cells):
            result[industry] = {}
            for skill in sorted(cells[industry]):
                cell = cells[industry][skill]
                placed = sorted(
                    candidate_id for candidate_id, vacancy_id in placements.items()
                    if vacancy_id in cell['vacancy_ids']
                )
                scenario = skill_rows[skill]['scenario']
                placed_via_skill = sorted(
                    item['candidate_id'] for item in scenario['placement_evidence']
                    if item['vacancy_id'] in cell['vacancy_ids'] and item['vacancy_requires_skill']
                )
                result[industry][skill] = {
                    'industry': industry,
                    'skill': skill,
                    'vacancy_count': len(cell['vacancy_ids']),
                    'vacancy_ids': sorted(cell['vacancy_ids']),
                    'candidate_missing_count': len(cell['missing_candidate_ids']),
                    'candidate_ids_missing': sorted(cell['missing_candidate_ids']),
                    'blocked_pair_count': len(cell['blocked_pairs']),
                    'blocked_pairs': sorted(cell['blocked_pairs']),
                    'skill_only_pairs': sorted(cell['skill_only_pairs']),
                    'mixed_constraint_pairs': sorted(cell['mixed_pairs']),
                    'baseline_placement_count': len(placed),
                    'baseline_placed_candidate_ids': placed,
                    'scenario_additional_placements_in_industry': len(placed_via_skill),
                    'scenario_placed_candidate_ids_in_industry': placed_via_skill,
                }
        return result

    def _skill_rows(
        self,
        candidates: Sequence[CandidateProfile],
        vacancies: Sequence[VacancyProfile],
        pairs: Mapping[tuple[str, str], PairAssessment],
        baseline: Mapping[str, str],
        exposures: Mapping[str, int],
        vacancy_views: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        vacancies_by_skill: dict[str, list[VacancyProfile]] = {}
        for vacancy in vacancies:
            for skill in vacancy.required_skills:
                vacancies_by_skill.setdefault(skill, []).append(vacancy)
        result: dict[str, Any] = {}
        for skill in sorted(vacancies_by_skill):
            related_vacancies = vacancies_by_skill[skill]
            related_ids = {item.vacancy_id for item in related_vacancies}
            blocked = [
                item for item in pairs.values()
                if item.vacancy_id in related_ids and skill in item.missing_skills
            ]
            skill_only = [item for item in blocked if set(item.missing_skills) == {skill}]
            candidates_missing = sorted({item.candidate_id for item in blocked})
            scenario = self._skill_scenario(
                skill, candidates, vacancies, pairs, baseline, exposures, vacancy_views
            )
            industry_counts: dict[str, int] = {}
            for vacancy in related_vacancies:
                industry_counts[vacancy.industry] = industry_counts.get(vacancy.industry, 0) + 1
            result[skill] = {
                'skill': skill,
                'vacancy_count': len(related_vacancies),
                'vacancy_ids': sorted(related_ids),
                'industry_counts': dict(sorted(industry_counts.items())),
                'candidate_missing_count': len(candidates_missing),
                'candidate_ids_missing': candidates_missing,
                'blocked_pair_count': len(blocked),
                'skill_only_blocked_pair_count': len(skill_only),
                'blocked_pair_evidence': [
                    {'candidate_id': item.candidate_id, 'vacancy_id': item.vacancy_id}
                    for item in sorted(blocked, key=lambda value: (value.candidate_id, value.vacancy_id))
                ],
                'scenario': scenario,
            }
        return result

    def _skill_scenario(
        self,
        skill: str,
        candidates: Sequence[CandidateProfile],
        vacancies: Sequence[VacancyProfile],
        pairs: Mapping[tuple[str, str], PairAssessment],
        baseline: Mapping[str, str],
        exposures: Mapping[str, int],
        vacancy_views: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        vacancy_by_id = {vacancy.vacancy_id: vacancy for vacancy in vacancies}
        # A candidate is a training target only if this skill is the last skill
        # gap for at least one vacancy; otherwise training it alone cannot
        # change any feasibility outcome.
        trained = sorted({
            candidate.candidate_id for candidate in candidates
            if skill not in candidate.skills and any(
                pair.candidate_id == candidate.candidate_id
                and skill in pair.missing_skills
                and set(pair.missing_skills) == {skill}
                for pair in pairs.values()
            )
        })
        scenario_views = [
            candidate.market_view(extra_skills=(skill,) if candidate.candidate_id in set(trained) else ())
            for candidate in candidates
        ]
        scenario_placements = self._clear(
            scenario_views,
            vacancy_views,
            exposures,
        )
        newly_placed = sorted(set(scenario_placements) - set(baseline))
        displaced = sorted(set(baseline) - set(scenario_placements))
        reassigned = sorted(
            candidate_id for candidate_id in set(baseline) & set(scenario_placements)
            if baseline[candidate_id] != scenario_placements[candidate_id]
        )

        newly_feasible: list[dict[str, Any]] = []
        still_blocked: list[dict[str, Any]] = []
        needs_more_skills: list[dict[str, Any]] = []
        for pair in pairs.values():
            if skill not in pair.missing_skills:
                continue
            if set(pair.missing_skills) != {skill}:
                needs_more_skills.append({
                    'candidate_id': pair.candidate_id,
                    'vacancy_id': pair.vacancy_id,
                    'industry': pair.industry,
                    'remaining_missing_skills': [
                        item for item in pair.missing_skills if item != skill
                    ],
                    'other_blockers': list(pair.other_blockers),
                    'other_categories': [
                        category for category in pair.categories if category != 'skill'
                    ],
                })
                continue
            if pair.other_blockers:
                still_blocked.append({
                    'candidate_id': pair.candidate_id,
                    'vacancy_id': pair.vacancy_id,
                    'industry': pair.industry,
                    'remaining_blockers': list(pair.other_blockers),
                    'remaining_categories': list(pair.categories),
                })
            else:
                newly_feasible.append({
                    'candidate_id': pair.candidate_id,
                    'vacancy_id': pair.vacancy_id,
                    'industry': pair.industry,
                })
        still_blocked_by_reason: dict[str, int] = {}
        for item in still_blocked:
            for reason in item['remaining_blockers']:
                still_blocked_by_reason[reason] = still_blocked_by_reason.get(reason, 0) + 1

        placement_evidence = []
        for candidate_id in newly_placed:
            vacancy_id = scenario_placements[candidate_id]
            vacancy = vacancy_by_id.get(vacancy_id)
            placement_evidence.append({
                'candidate_id': candidate_id,
                'vacancy_id': vacancy_id,
                'industry': vacancy.industry if vacancy else None,
                'vacancy_requires_skill': bool(
                    vacancy and skill in vacancy.required_skills
                ),
            })
        feasible_but_unplaced = sorted({
            item['candidate_id'] for item in newly_feasible
            if item['candidate_id'] not in scenario_placements
        })
        return {
            'skill': skill,
            'trained_candidate_count': len(trained),
            'trained_candidate_ids': trained,
            'newly_feasible_pair_count': len(newly_feasible),
            'newly_feasible_candidates': sorted({item['candidate_id'] for item in newly_feasible}),
            'newly_feasible_pair_evidence': sorted(
                newly_feasible, key=lambda item: (item['candidate_id'], item['vacancy_id'])
            ),
            'newly_placed_count': len(newly_placed),
            'additional_placements': len(newly_placed),
            'newly_placed_candidates': newly_placed,
            'placement_evidence': sorted(
                placement_evidence, key=lambda item: (item['candidate_id'], item['vacancy_id'])
            ),
            'net_placement_change': len(scenario_placements) - len(baseline),
            'displaced_candidate_ids': displaced,
            'reassigned_candidate_ids': reassigned,
            'newly_feasible_but_unplaced_candidates': feasible_but_unplaced,
            'still_blocked_pair_count': len(still_blocked),
            'still_blocked_by_reason': dict(sorted(still_blocked_by_reason.items())),
            'still_blocked_pair_evidence': sorted(
                still_blocked, key=lambda item: (item['candidate_id'], item['vacancy_id'])
            ),
            'needs_more_skills_pair_count': len(needs_more_skills),
            'needs_more_skills_pair_evidence': sorted(
                needs_more_skills, key=lambda item: (item['candidate_id'], item['vacancy_id'])
            ),
            'baseline_placement_count': len(baseline),
            'scenario_placement_count': len(scenario_placements),
            'scenario_placements': dict(sorted(scenario_placements.items())),
        }

    def _ranking(self, skill_rows: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for skill in sorted(skill_rows):
            item = skill_rows[skill]
            scenario = item['scenario']
            trained = scenario['trained_candidate_count']
            net = scenario['net_placement_change']
            rows.append({
                'skill': skill,
                'additional_placements': scenario['additional_placements'],
                'net_placement_change': net,
                'newly_placed_candidate_count': scenario['newly_placed_count'],
                'displaced_candidate_count': len(scenario['displaced_candidate_ids']),
                'newly_feasible_pair_count': scenario['newly_feasible_pair_count'],
                'still_blocked_pair_count': scenario['still_blocked_pair_count'],
                'needs_more_skills_pair_count': scenario['needs_more_skills_pair_count'],
                'newly_feasible_but_unplaced_count': len(
                    scenario['newly_feasible_but_unplaced_candidates']
                ),
                'trained_candidate_count': trained,
                'net_placements_per_trained_candidate': (
                    round(net / trained, 4) if trained else 0.0
                ),
                'vacancy_count': item['vacancy_count'],
                'blocked_pair_count': item['blocked_pair_count'],
                'industry_counts': dict(item['industry_counts']),
            })
        # Primary key is net employment created under a capacity-aware clearing,
        # so a skill that merely reshuffles an existing placement does not
        # outrank one that adds a genuinely new placement.  Among skills with
        # equal net employment, the one requiring fewer trained candidates wins
        # -- it is the cheaper use of a fixed quota.  Vacancy demand is only a
        # later tie-breaker, never the basis of the ranking.
        rows.sort(key=lambda row: (
            -row['net_placement_change'],
            -row['net_placements_per_trained_candidate'] if row['trained_candidate_count'] else 0,
            -row['additional_placements'],
            row['displaced_candidate_count'],
            -row['newly_feasible_pair_count'],
            -row['blocked_pair_count'],
            row['skill'],
        ))
        for index, row in enumerate(rows, 1):
            row['rank'] = index
        return rows

    def _summary(
        self,
        candidates: Sequence[CandidateProfile],
        vacancies: Sequence[VacancyProfile],
        pairs: Mapping[tuple[str, str], PairAssessment],
        candidate_rows: Sequence[Mapping[str, Any]],
        baseline: Mapping[str, str],
        ranking: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        blocked = [item for item in pairs.values() if not item.feasible]
        blockers_by_reason: dict[str, int] = {}
        categories: dict[str, int] = {}
        for item in blocked:
            for reason in item.blockers:
                blockers_by_reason[reason] = blockers_by_reason.get(reason, 0) + 1
            for category in item.categories:
                categories[category] = categories.get(category, 0) + 1
        skill_only = sum(
            1 for item in blocked if bool(item.missing_skills) and item.categories == ('skill',)
        )
        skill_and_other = sum(
            1 for item in blocked if item.missing_skills and len(item.categories) > 1
        )
        other_only = sum(1 for item in blocked if not item.missing_skills)
        unplaced_by_status: dict[str, int] = {}
        for row in candidate_rows:
            if row['status'] == 'placed':
                continue
            unplaced_by_status[row['status']] = unplaced_by_status.get(row['status'], 0) + 1
        capacity_exhausted = sorted(
            row['candidate_id'] for row in candidate_rows
            if row['status'] == 'capacity_exhausted'
        )
        return {
            'candidate_count': len(candidates),
            'vacancy_count': len(vacancies),
            'pair_count': len(pairs),
            'feasible_pair_count': len(pairs) - len(blocked),
            'blocked_pair_count': len(blocked),
            'blocked_pairs_by_reason': dict(sorted(blockers_by_reason.items())),
            'blocked_pairs_by_category': dict(sorted(categories.items())),
            'pairs_skill_only': skill_only,
            'pairs_skill_and_other': skill_and_other,
            'pairs_other_only': other_only,
            'baseline_placement_count': len(baseline),
            'baseline_placed_candidates': sorted(baseline),
            'baseline_unplaced_candidates': sorted(
                row['candidate_id'] for row in candidate_rows if row['status'] != 'placed'
            ),
            'unplaced_by_status': dict(sorted(unplaced_by_status.items())),
            'capacity_exhausted_candidates': capacity_exhausted,
            'capacity_exhausted_count': len(capacity_exhausted),
            'skills_ranked': [row['skill'] for row in ranking],
            'ranking_basis': 'additional distinct placements under capacity-aware clearing',
        }


def analyze_skill_gaps(
    candidates: Iterable[Mapping[str, Any]],
    vacancies: Iterable[Mapping[str, Any]],
    exposures: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    include_pair_evidence: bool = True,
    reserved: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper returning a JSON-friendly mapping."""
    return SkillGapAnalyzer().analyze(
        candidates, vacancies, exposures,
        include_pair_evidence=include_pair_evidence, reserved=reserved,
    ).as_dict()


def load_database_input(database: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Read candidates, vacancies and exposure counts from storage.

    This function performs only ``list_records`` reads.  It never mutates
    records, events, outbox rows or exposure history.
    """
    candidates = database.list_records('candidate')
    vacancies = database.list_records('vacancy')
    exposure_counts: dict[str, int] = {}
    for row in database.list_records('exposure'):
        attributes = row.get('attributes') or {}
        vacancy_id = attributes.get('vacancy_id') or attributes.get('vacancy')
        if vacancy_id is None:
            continue
        key = str(vacancy_id)
        exposure_counts[key] = exposure_counts.get(key, 0) + int(row.get('quantity', 0))
    return candidates, vacancies, exposure_counts


__all__ = [
    'CATEGORY_ORDER',
    'CATEGORY_REASONS',
    'CandidateProfile',
    'PairAssessment',
    'REASON_CATEGORIES',
    'SkillGapAnalyzer',
    'SkillGapError',
    'SkillGapReport',
    'VacancyProfile',
    'analyze_skill_gaps',
    'candidate_profile',
    'load_database_input',
    'vacancy_profile',
]
