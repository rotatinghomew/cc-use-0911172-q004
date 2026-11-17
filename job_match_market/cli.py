from __future__ import annotations
import argparse
import json
from pathlib import Path
from .analytics.skill_gap import SkillGapAnalyzer, load_database_input
from .reporting import Reporter
from .service import ApplicationService
from .storage import Database

def load_json(path:str):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def dump_json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)

def main(argv=None):
    parser=argparse.ArgumentParser(prog='job_match_market')
    parser.add_argument('--database',default=':memory:')
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run'); run.add_argument('--fixture',required=True); run.add_argument('--output',required=True)
    report=sub.add_parser('report'); report.add_argument('--kind')
    skill_gap=sub.add_parser('skill-gap'); skill_gap.add_argument('--output'); skill_gap.add_argument('--no-pair-evidence',action='store_true')
    skill_gap.add_argument('--fixture',help='JSON with candidates/vacancies/context instead of reading the database')
    skill_gap.add_argument('--reserved-file',help='JSON object mapping vacancy_id to already-committed slots (read-only assumption)')
    args=parser.parse_args(argv); database=Database(args.database)
    if args.command=='run':
        fixture=load_json(args.fixture); service=ApplicationService(database); outputs=[]
        for index,item in enumerate(fixture.get('items',[])):
            outputs.append(service.process(item,fixture.get('context',{}),item.get('request_key',f'item-{index}')))
        payload={'product':'来沈青年岗位匹配与曝光公平交易台','processed':len(outputs),'outputs':outputs,'report':Reporter(database).overview()}
        Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(payload,ensure_ascii=False,sort_keys=True)); return 0
    if args.command=='skill-gap':
        # Read-only: fixture input never touches the database; database input
        # only calls list_records.  Nothing is written back either way.
        if args.fixture:
            fixture=load_json(args.fixture)
            candidates=fixture.get('candidates',[])
            vacancies=fixture.get('vacancies',[])
            context=fixture.get('context',{})
            exposures=fixture.get('exposures',context.get('exposures'))
        else:
            candidates,vacancies,exposures=load_database_input(database)
            context={}
        reserved=load_json(args.reserved_file) if args.reserved_file else context.get('reserved')
        payload=SkillGapAnalyzer().analyze(candidates,vacancies,exposures,include_pair_evidence=not args.no_pair_evidence,reserved=reserved).as_dict()
        if args.output:
            Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(dump_json({'product':'来沈青年岗位匹配与曝光公平交易台','command':'skill-gap','summary':payload['summary'],'skill_ranking':payload['skill_ranking']})); return 0
    payload=Reporter(database).owner_breakdown(args.kind) if args.kind else Reporter(database).overview(); print(json.dumps(payload,ensure_ascii=False,sort_keys=True)); return 0
