"""定时脚本和 launchd 模板的静态检查。

Mac mini 上的 /bin/bash 是 3.2：变量后面紧跟全角字符时，会把那几个字节算进变量名，
set -u 下直接报错（备份脚本就这样出过错，Linux 上的 bash 5 测不出来）。这里逐行查，
要求一律写成 ${变量}。
"""

import importlib.util
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((ROOT / "scripts").glob("*.sh"))
PLISTS = sorted((ROOT / "deploy" / "launchd").glob("*.plist"))
_GLUED = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]")


def test_there_are_scripts_to_check():
    assert SCRIPTS and PLISTS


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_no_variable_glued_to_non_ascii(path: Path):
    bad = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if _GLUED.search(line):
            bad.append(f"{path.name}:{number}: {line.strip()}")
    assert not bad, "变量后面紧跟中文，写成 ${变量}：\n" + "\n".join(bad)


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_scripts_parse(path: Path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("没有 bash")
    result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", [path for path in SCRIPTS if path.name != "status.sh"], ids=lambda path: path.name)
def test_scripts_are_executable(path: Path):
    # 文档里都写成直接跑 scripts/xxx.sh；status.sh 只用 . 引入，不单独跑。
    assert path.stat().st_mode & 0o100, f"{path.name} 没有执行权限：git update-index --chmod=+x scripts/{path.name}"


@pytest.mark.parametrize("path", PLISTS, ids=lambda path: path.name)
def test_launchd_templates_are_valid_plists(path: Path):
    filled = (
        path.read_text(encoding="utf-8")
        .replace("__REPO__", "/Users/me/research")
        .replace("__BACKUP_DIR__", "/Users/me/research-backups")
        .replace("__LOGS__", "/Users/me/Library/Logs/research")
        .replace("__PATH__", "/opt/homebrew/bin:/usr/bin:/bin")
        .replace("__NIGHTLY_OCR__", "0")
    )
    assert "__" not in re.sub(r"<!--.*?-->", "", filled, flags=re.S)
    data = plistlib.loads(filled.encode("utf-8"))
    assert data["Label"] == path.stem
    assert data["ProgramArguments"][0] == "/bin/bash"
    script = data["ProgramArguments"][1].replace("/Users/me/research/", "")
    assert (ROOT / script).is_file()


def test_ci_failure_becomes_one_annotation_with_the_tail(capsys):
    spec = importlib.util.spec_from_file_location("ci_annotate", ROOT / "tools" / "ci" / "annotate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    failing = "import sys; print('第一行'); print('50%：失败, 见上'); sys.exit(3)"
    assert module.main(["单元测试", "--", sys.executable, "-c", failing]) == 3
    out = capsys.readouterr().out
    assert "第一行\n" in out
    assert "::error title=单元测试（退出码 3）::第一行%0A50%25：失败, 见上" in out
    assert module.main(["好的", "--", sys.executable, "-c", "print('ok')"]) == 0
    assert "::error" not in capsys.readouterr().out


def test_nightly_starts_app_before_ocr():
    """识图在 app 容器里跑。容器没起来时先 up，避免 service app is not running。"""
    nightly = (ROOT / "scripts" / "nightly.sh").read_text(encoding="utf-8")
    status = (ROOT / "scripts" / "status.sh").read_text(encoding="utf-8")
    assert "compose_exec app research-ocr" in nightly
    assert "compose up -d" in status
    assert "is not running" in status
    commands = {
        "crawl-fangwei-mp.sh": "crawl_with_receipt research-crawl-wechat",
        "crawl-gudujinhuati.sh": "crawl_with_receipt research-crawl-wechat",
        "crawl-latepost.sh": "crawl_with_receipt research-crawl-wechat",
        "crawl-fangwei-zsxq.sh": "crawl_with_receipt research-ingest",
    }
    for name, command in commands.items():
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert command in text


def test_fangwei_mp_crawl_takes_bodies_from_the_original_page():
    """方伟公众号和孤独进化体同一条路：research-crawl-wechat 从原文页取正文，不再跑仓库外的 dl.py。
    以前入库的老文章，每次再按原文页把图摆回原位一批。"""
    text = (ROOT / "scripts" / "crawl-fangwei-mp.sh").read_text(encoding="utf-8")
    assert 'research-crawl-wechat --source "$SOURCE"' in text and "SOURCE=fangwei-mp" in text
    assert "python3" not in text and 'dl.py"' not in text
    steps = ("crawl_with_receipt research-crawl-wechat", "compose_exec app research-place-wechat-images",
             "compose_exec app research-fetch-images")
    order = [text.index(step) for step in steps]
    order.append(text.index('process_new "$SOURCE"'))
    assert order == sorted(order)
    # 老文章的图每次按原文页摆一批，导入之后跑（导入整目录时库里摆好的留着）。
    assert 'research-place-wechat-images --source "$SOURCE" --limit "$PLACE_LIMIT"' in text


def test_nightly_reads_images_before_views_and_digests():
    """识图紧跟外链图片，排在向量、观点、摘要前面：当天的截图帖第一遍就带着图里的字抽。"""
    nightly = (ROOT / "scripts" / "nightly.sh").read_text(encoding="utf-8")
    steps = ("research-fetch-images", "research-ocr", "research-embed", "research-views", "research-digest")
    order = [nightly.index(f"compose_exec app {name}") for name in steps]
    assert order == sorted(order)


def test_nightly_counts_mostly_failed_image_downloads_as_a_failed_step():
    """11-09 夜里外链图片 300 个新地址一张没下下来，夜间补齐却报成功。夜间补齐带 --exit-code：新地址大半下不来时
    research-fetch-images 退出码 3，step 记成失败。定时抓取不带：run_step 一失败就退出，下图有问题会连后面的摘要都不补，
    那边的结论看 fetch-images.json（research-status 照样列出来）。"""
    nightly = (ROOT / "scripts" / "nightly.sh").read_text(encoding="utf-8")
    assert 'step "外链图片" compose_exec app research-fetch-images --limit "$IMAGES_LIMIT" --exit-code' in nightly
    for name in ("crawl-fangwei-mp.sh", "crawl-gudujinhuati.sh", "crawl-latepost.sh"):
        script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "research-fetch-images --limit 200" in script and "--exit-code" not in script, name


def test_latepost_crawl_is_incremental_and_does_not_backfill():
    """定时脚本只查最新一页。历史回填不在这里，部署和 launchd 都不会翻页。"""
    text = (ROOT / "scripts" / "crawl-latepost.sh").read_text(encoding="utf-8")
    assert "SOURCE=latepost" in text
    assert 'MAX_NEW="${MAX_NEW:-10}"' in text
    assert '--max-new "$MAX_NEW"' in text
    commands = "\n".join(line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    assert "--backfill" not in commands and "--list-only" not in commands
    # 只当场处理这次抓进来的，不按来源补积压（回填导入的几千篇由夜间按上限补）。
    assert "research-digest" not in commands and 'process_new "$SOURCE"' in commands
    plist = (ROOT / "deploy" / "launchd" / "com.zldong.research.crawl-latepost.plist").read_text(encoding="utf-8")
    assert plist.count("<integer>30</integer>") == 2
    assert "<integer>8</integer>" in plist and "<integer>20</integer>" in plist
    assert "crawl-latepost.sh" in plist
    assert "crawl-latepost" in (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "crawl-latepost" in (ROOT / "scripts" / "install-launchd.sh").read_text(encoding="utf-8")


def test_crawls_process_new_articles_right_after_import():
    """Zhilin 11-19：新抓的译制、判断、摘要、打分不等夜间、不等便宜时段。每个抓取脚本用自己的回执，
    导入、下图以后对这次新进来的跑 research-process-new；只处理新的，不按来源补积压。"""
    scripts = {
        "crawl-fangwei-mp.sh": ("research-crawl-wechat", '"$SOURCE"'),
        "crawl-gudujinhuati.sh": ("research-crawl-wechat", '"$SOURCE"'),
        "crawl-latepost.sh": ("research-crawl-wechat", '"$SOURCE"'),
        "crawl-fangwei-zsxq.sh": ("research-ingest-zsxq", "fangwei-zsxq"),
        "crawl-stratechery.sh": ("research-crawl-stratechery", '"$SOURCE"'),
    }
    for name, (importer, source) in scripts.items():
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        commands = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        imported = commands.index(f"crawl_with_receipt {importer}")
        processed = commands.index(f"process_new {source}")
        assert imported < processed, name
        assert "research-digest" not in commands, name
        if "research-fetch-images" in commands:
            assert commands.index("research-fetch-images") < processed, name
    nightly = (ROOT / "scripts" / "nightly.sh").read_text(encoding="utf-8")
    crawl = nightly.index("crawl_with_receipt research-crawl-stratechery")
    fresh = nightly.index('process_new stratechery')
    assert crawl < fresh < nightly.index("research-translate --source stratechery")
    status = (ROOT / "scripts" / "status.sh").read_text(encoding="utf-8")
    assert "RESEARCH_CRAWL_RECEIPT=" in status and "--receipt" in status
    # 不带 --off-peak：新文章不等便宜时段。
    assert "--off-peak" not in status


def test_stratechery_is_also_crawled_in_the_evening():
    """Zhilin 11-20：Stratechery 白天再抓一次。21:15 跑，没设 RSS 地址时跳过记成功，自动上线等它跑完。"""
    text = (ROOT / "scripts" / "crawl-stratechery.sh").read_text(encoding="utf-8")
    assert "SOURCE=stratechery" in text and "TASK=crawl-stratechery" in text
    assert "STRATECHERY_RSS_URL" in text and 'status_ok "跳过' in text
    plist = (ROOT / "deploy" / "launchd" / "com.zldong.research.crawl-stratechery.plist").read_text(encoding="utf-8")
    assert "<integer>21</integer>" in plist and "<integer>15</integer>" in plist
    assert "crawl-stratechery.sh" in plist and "crawl-stratechery.log" in plist
    assert "crawl-stratechery" in (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    install = (ROOT / "scripts" / "install-launchd.sh").read_text(encoding="utf-8")
    assert install.count("crawl-stratechery") >= 4


def test_nightly_theses_disabled_by_default():
    """论点夜间步骤默认关：没设 THESES_ENABLED=1 时不跑 research-theses。"""
    source = (ROOT / "scripts" / "nightly.sh").read_text(encoding="utf-8")
    assert 'THESES_ENABLED="${THESES_ENABLED:-0}"' in source
    assert 'if [ "$THESES_ENABLED" = "1" ]; then' in source
    assert "research-theses" in source
    # 默认关时 status_ok 不硬编码带「论点」。
    assert 'status_ok "外链图片、观点、摘要、论点' not in source



def test_zsxq_crawler_lives_in_the_repo_without_the_group_id(tmp_path):
    """星球抓取脚本收进仓库（审核 O3）：数据写进 ZSXQ_ARCHIVE 指的目录，不写进仓库；星球 ID 不写死。"""
    import subprocess
    import sys

    dl = ROOT / "scripts" / "zsxq" / "dl.py"
    assert dl.stat().st_mode & 0o111
    text = dl.read_text(encoding="utf-8")
    assert "ZSXQ_ACCESS_TOKEN=" in text and "read_token" in text
    assert not re.search(r"\d{12,}", text), "星球 ID、账户 ID 不写进公开仓库"
    crawl = (ROOT / "scripts" / "crawl-fangwei-zsxq.sh").read_text(encoding="utf-8")
    assert 'DL="${DL:-$REPO/scripts/zsxq/dl.py}"' in crawl
    assert 'ZSXQ_ARCHIVE="$ARCHIVE" python3 "$DL"' in crawl
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ZSXQ_ENV": str(tmp_path / "none.env")}
    # 不给存档目录不跑。
    done = subprocess.run([sys.executable, str(dl)], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 2 and "ZSXQ_ARCHIVE" in done.stderr
    # 给了目录、没有星球 ID 也不跑，什么都不写。
    archive = tmp_path / "archive"
    env["ZSXQ_ARCHIVE"] = str(archive)
    done = subprocess.run([sys.executable, str(dl)], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 2 and "ZSXQ_GROUP_ID" in done.stderr and not archive.exists()
    # 只渲染时不需要星球 ID：在存档目录里写 md/ 和 README，仓库里不多文件。
    done = subprocess.run([sys.executable, str(dl), "--render"], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert (archive / "md" / "INDEX.md").exists() and (archive / "README.md").exists()
    assert not (ROOT / "scripts" / "zsxq" / "raw").exists()


def test_nightly_scores_new_passages_after_digests():
    nightly = (ROOT / "scripts" / "nightly.sh").read_text(encoding="utf-8")
    assert 'step "段落打分" compose_exec app research-mechanism --limit "$MECHANISM_LIMIT"' in nightly
    assert nightly.index("app research-digest") < nightly.index("app research-mechanism")


def test_crawl_and_process_share_only_this_runs_receipt(tmp_path):
    script = r"""
    set -euo pipefail
    TASK=test
    REPO="$1"
    . "$2"
    new_ocr_enabled() { return 1; }
    compose_exec() {
      shift # app
      case "$1" in
        env)
          test "$2" = "RESEARCH_CRAWL_RECEIPT=/data/${CRAWL_RECEIPT##*/}"
          test "$3" = "research-crawl-wechat"
          printf '42\n' > "$CRAWL_RECEIPT"
          ;;
        research-process-new)
          test "$2" = "--source" && test "$3" = "latepost"
          test "$4" = "--receipt" && test "$5" = "/data/${CRAWL_RECEIPT##*/}"
          test "$(cat "$CRAWL_RECEIPT")" = "42"
          ;;
        research-crawl-wechat) test "$PROCESS_NEW" = "0" ;;
        *) return 1 ;;
      esac
    }
    run_step "导入" crawl_with_receipt research-crawl-wechat
    first="$CRAWL_RECEIPT"
    run_step "处理" process_new latepost
    (
      run_step "导入" crawl_with_receipt research-crawl-wechat
      test "$CRAWL_RECEIPT" != "$first"
      status_finish 0
    )
    test -f "$first"
    status_finish 0
    test ! -e "$first"
    PROCESS_NEW=0
    run_step "只导入" crawl_with_receipt research-crawl-wechat
    run_step "跳过" process_new latepost
    """
    subprocess.run(["bash", "-c", script, "test", str(tmp_path), str(ROOT / "scripts/status.sh")], check=True)
