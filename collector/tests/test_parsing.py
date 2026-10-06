from dahua_monitor import parsing

SAMPLE = """\
list.info[0].Name=/dev/sda
list.info[0].State=Success
list.info[0].HealthDataFlag=true
list.info[0].Detail[0].Type=ReadWrite
list.info[0].Detail[0].TotalBytes=6001175126016
list.info[0].Detail[0].UsedBytes=3000587563008
list.info[0].Detail[0].IsError=false
list.info[1].Name=/dev/md0
list.info[1].State=Degraded
list.info[1].Type=Raid5
list.info[1].Detail[0].TotalBytes=12002350252032
list.info[1].Detail[0].UsedBytes=6001175126016
list.info[1].Detail[0].IsError=false
"""


def test_parse_kv_tree_nested():
    tree = parsing.parse_kv_tree(SAMPLE)
    infos = parsing.storage_infos(tree)
    assert len(infos) == 2
    assert infos[0]["Name"] == "/dev/sda"
    assert infos[0]["HealthDataFlag"] is True
    assert infos[0]["Detail"][0]["TotalBytes"] == 6001175126016
    assert infos[1]["Type"] == "Raid5"


def test_parse_kv_tree_tolerates_junk():
    tree = parsing.parse_kv_tree("garip satir\nError\n\nlist.info[0].Name=/dev/sda\n")
    assert parsing.storage_infos(tree)[0]["Name"] == "/dev/sda"


def test_parse_kv_tree_without_list_prefix():
    tree = parsing.parse_kv_tree("info[0].Name=/dev/sda\ninfo[0].State=Success\n")
    infos = parsing.storage_infos(tree)
    assert infos[0]["State"] == "Success"


def test_parse_flat():
    flat = parsing.parse_flat("version=3.216.0000000.1\r\nsn=ABC123\r\n")
    assert flat["version"] == "3.216.0000000.1"
    assert flat["sn"] == "ABC123"


def test_coercion():
    tree = parsing.parse_kv_tree("a.b=12\na.c=1.5\na.d=true\na.e=metin\n")
    assert tree["a"] == {"b": 12, "c": 1.5, "d": True, "e": "metin"}
