import pytest

from cmk_base.check_api import MKCounterWrapped

pytestmark = pytest.mark.checks

_broken_info = [[
    'DB19',
    ' Debug (121): ORA-01219: database or pluggable database not open: queries allowed on fixed tables or views only'
]]


@pytest.mark.parametrize('info', [
    _broken_info,
])
def test_oracle_jobs_discovery_error(check_manager, info):
    check = check_manager.get_check('oracle_jobs')
    assert list(check.run_discovery(info)) == []


@pytest.mark.parametrize('info', [
    _broken_info,
])
def test_oracle_jobs_check_error(check_manager, info):
    check = check_manager.get_check('oracle_jobs')
    with pytest.raises(MKCounterWrapped):
        check.run_check("DB19.SYS.JOB1", {}, info)


_STRING_TABLE_CDB_NONCDB = [
    [
        'CDB',
        'CDB$ROOT',
        'SYS',
        'AUTO_SPACE_ADVISOR_JOB',
        'SCHEDULED',
        '0',
        '46',
        'TRUE',
        '15-JUN-21 01.01.01.143871 AM +00:00',
        '-',
        'SUCCEEDED',
    ],
    [
        'NONCDB',
        'SYS',
        'AUTO_SPACE_ADVISOR_JOB',
        'SCHEDULED',
        '995',
        '1129',
        'TRUE',
        '16-JUN-21 01.01.01.143871 AM +00:00',
        'MAINTENANCE_WINDOW_GROUP',
        '',
    ],
]


def test_discovery_cdb_noncdb(check_manager):
    assert list(check_manager.get_check('oracle_jobs').run_discovery(_STRING_TABLE_CDB_NONCDB)) == [
        (
            'CDB.CDB$ROOT.SYS.AUTO_SPACE_ADVISOR_JOB',
            {},
        ),
        (
            'NONCDB.SYS.AUTO_SPACE_ADVISOR_JOB',
            {},
        ),
    ]


@pytest.mark.parametrize(
    "item, result",
    [
        pytest.param(
            'CDB.CDB$ROOT.SYS.AUTO_SPACE_ADVISOR_JOB',
            (
                0,
                'Job-State: SCHEDULED, Enabled: Yes, Last Duration: 0.00 s, Next Run: 15-JUN-21 01.01.01.143871 AM +00:00, Last Run Status: SUCCEEDED (ignored disabled Job)',
                [
                    ('duration', 0),
                ],
            ),
            id="cdb",
        ),
        pytest.param(
            'NONCDB.SYS.AUTO_SPACE_ADVISOR_JOB',
            (
                1,
                'Job-State: SCHEDULED, Enabled: Yes, Last Duration: 16 m, Next Run: 16-JUN-21 01.01.01.143871 AM +00:00,  no log information found(!)',
                [
                    ('duration', 995),
                ],
            ),
            id="noncdb",
        ),
    ],
)
def test_check_cdb_noncdb(
        check_manager,
        item,
        result,
):
    assert check_manager.get_check('oracle_jobs').run_check(
        item,
        {
            'disabled': False,
            'status_missing_jobs': 2,
            'missinglog': 1,
        },
        _STRING_TABLE_CDB_NONCDB,
    ) == result
