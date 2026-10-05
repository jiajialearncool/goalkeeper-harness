"""Developer fixtures. These are never reused or renamed as formal unseen questions."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .benchmark import Case, Feedback
from .models import CheckSpec, Goal


def write(root: Path, files: dict[str, str]):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def prepare(package: Path, force: bool = False) -> dict:
    if (package / "manifest.json").exists() and not force:
        return json.loads((package / "manifest.json").read_text())
    package.mkdir(parents=True, exist_ok=True)
    cases = []

    def add(case_id, category, title, goal, initial, reference, tests, names, feedback=None, ports=None):
        initial = {
            **initial,
            "TASK.md": goal + "\n",
            "public_check.py": 'from pathlib import Path\nfor p in Path(".").glob("*.py"):\n    compile(p.read_text(),str(p),"exec")\n',
        }
        reference = {**initial, **reference}
        prefix = Path(case_id)
        write(package / prefix / "initial", initial)
        write(package / prefix / "reference", reference)
        write(package / prefix / "mutants/unmodified", initial)
        # A separate behavioral counterexample leaves the requested structural edits in place.
        faulty = dict(reference)
        module = next((n for n in reference if n.endswith(".py") and n not in {"public_check.py"}), None)
        if module:
            faulty[module] += '\nraise RuntimeError("deliberate regression counterexample")\n'
        else:
            module = (
                "src/main.js"
                if "src/main.js" in reference
                else next(n for n in reference if n.endswith((".js", ".jsx")))
            )
            faulty[module] += '\nthrow new Error("deliberate regression counterexample");\n'
        write(package / prefix / "mutants/regression", faulty)
        grader_file = str(prefix / "test_independent.py")
        write(package, {grader_file: tests})
        cases.append(
            Case(
                id=case_id,
                category=category,
                title=title,
                initial=str(prefix / "initial"),
                reference=str(prefix / "reference"),
                mutants=[str(prefix / "mutants/unmodified"), str(prefix / "mutants/regression")],
                goal=Goal(original=goal, current=goal),
                grader_file=grader_file,
                expected_tests=names,
                public_checks=[CheckSpec(name="语法检查", argv=[sys.executable, "public_check.py"])],
                editable=sorted((set(initial) | set(reference)) - {"TASK.md"}) + ["tests/"],
                feedback=feedback,
                ports=ports or [],
                environment_ready=True,
            )
        )

    initial, reference = (
        {},
        {
            "log_config.py": 'import logging\ndef configure(level):\n    logging.getLogger("sales").setLevel(level)\n'
        },
    )
    for i in range(12):
        initial[f"unit{i}.py"] = (
            f'def calculate(value):\n    print("unit{i}",value)\n    return round(value*{i + 1},2)\n'
        )
        reference[f"unit{i}.py"] = (
            f'import logging\nlogger=logging.getLogger("sales")\ndef calculate(value):\n    logger.info("unit{i} %s",value)\n    return round(value*{i + 1},2)\n'
        )
    add(
        "A1",
        "refactor",
        "统一十二个业务模块的日志并保留计算结果",
        "把 unit0.py 到 unit11.py 的 print 改为标准 logging；日志分级并可关闭。实现 log_config.configure(level)。保持每个 calculate 的计算结果、异常行为和模块入口。",
        initial,
        reference,
        """import ast,importlib,logging
from pathlib import Path
def test_behavior():
    for i in range(12):
        mod=importlib.import_module(f'unit{i}')
        for value in [0,1.25,-4.5]: assert mod.calculate(value)==round(value*(i+1),2)
def test_no_print():
    for i in range(12):
        tree=ast.parse(Path(f'unit{i}.py').read_text())
        assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='print' for n in ast.walk(tree))
def test_log_level_and_off_switch(monkeypatch):
    from log_config import configure
    records=[]
    original=logging.Logger.handle
    def capture(logger,record):
        records.append(record)
        return original(logger,record)
    monkeypatch.setattr(logging.Logger,'handle',capture)
    configure(logging.DEBUG)
    for i in range(12):
        before=len(records)
        importlib.import_module(f'unit{i}').calculate(1)
        assert len(records)>before
    assert all(logging.DEBUG<=r.levelno<=logging.CRITICAL for r in records)
    records.clear();configure(logging.CRITICAL+1)
    for i in range(12):importlib.import_module(f'unit{i}').calculate(1)
    assert records==[]
""",
        ["test_behavior", "test_no_print", "test_log_level_and_off_switch"],
    )

    node_modules = Path(__file__).resolve().parents[2] / "frontend/node_modules"
    react = str(node_modules / "react/index.js")
    renderer = str(node_modules / "react-test-renderer/index.js")
    typescript = str(node_modules / "typescript/lib/typescript.js")
    # Pure JSX files are compiled by the immutable grader; candidate doesn't see hidden test inputs.
    initial, reference = {}, {}
    for i in range(6):
        initial[f"Counter{i}.jsx"] = f"""import React from '{react}';
export default class Counter extends React.Component {{
 constructor(props){{super(props);this.state={{count:props.start}};}}
 render(){{return <button onClick={{()=>this.setState({{count:this.state.count+1}})}}>{{this.props.label}}:{{this.state.count}}</button>}}
}}
"""
        reference[f"Counter{i}.jsx"] = f"""import React,{{useState}} from '{react}';
export default function Counter(props){{const [count,setCount]=useState(props.start);return <button onClick={{()=>setCount(c=>c+1)}}>{{props.label}}:{{count}}</button>}}
"""
    react_test = f'''import json,subprocess
from pathlib import Path
REACT={react!r};RENDERER={renderer!r};TS={typescript!r}
def test_no_class_components():
    for i in range(6):assert 'class Counter' not in Path(f'Counter{{i}}.jsx').read_text()
def test_hooks_and_state_behavior():
    program="""const fs=require('fs'),React=require(process.argv[1]),R=require(process.argv[2]),ts=require(process.argv[3]);global.IS_REACT_ACT_ENVIRONMENT=true;
async function main(){{for(let i=0;i<6;i++){{let src=fs.readFileSync('Counter'+i+'.jsx','utf8');let js=ts.transpileModule(src,{{compilerOptions:{{jsx:ts.JsxEmit.React,module:ts.ModuleKind.CommonJS,esModuleInterop:true}}}}).outputText;let module={{exports:{{}}}};new Function('require','module','exports',js)(require,module,module.exports);let C=module.exports.default;let instance;await R.act(async()=>{{instance=R.create(React.createElement(C,{{start:7,label:'A'}}))}});if(instance.toJSON().children.join('')!=='A:7')throw Error('initial');await R.act(async()=>instance.root.findByType('button').props.onClick());if(instance.toJSON().children.join('')!=='A:8')throw Error('increment');await R.act(async()=>instance.update(React.createElement(C,{{start:100,label:'B'}})));if(instance.toJSON().children.join('')!=='B:8')throw Error('prop update');await R.act(async()=>instance.unmount());}}}}main().catch(e=>{{console.error(e);process.exitCode=1}});"""
    result=subprocess.run(['/usr/local/bin/node','-e',program,REACT,RENDERER,TS],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
'''
    add(
        "A2",
        "refactor",
        "把六个计数组件改为函数组件并保留状态行为",
        "将六个 Counter JSX class 组件转换为函数组件和 hooks。保留初始计数、点击增加、属性更新后计数不重置的行为。禁止 class 残留。",
        initial,
        reference,
        react_test,
        ["test_no_class_components", "test_hooks_and_state_behavior"],
    )

    initial, reference = {}, {}
    for i in range(8):
        initial[f"flow{i}.js"] = (
            f"exports.process=(fetcher,value)=>fetcher(value).then(x=>Promise.resolve(x*{i + 1}).then(y=>y+1)).catch(e=>({{error:e.message}}));\n"
        )
        reference[f"flow{i}.js"] = (
            f"exports.process=async(fetcher,value)=>{{try{{const x=await fetcher(value);const y=await Promise.resolve(x*{i + 1});return y+1}}catch(e){{return {{error:e.message}}}}}};\n"
        )
    add(
        "A3",
        "refactor",
        "转换八条异步流程并保留失败返回值",
        "把八个 flow 的 then/catch 链改为 async/await 和 try/catch。保留成功返回值与异常信息，不得增加吞掉错误的空 catch。",
        initial,
        reference,
        """import json,subprocess
from pathlib import Path
def test_no_promise_chains():
    for i in range(8):
        text=Path(f'flow{i}.js').read_text();assert '.then(' not in text and '.catch(' not in text and 'await' in text
def test_success_and_failure():
    program="(async()=>{const r=[];for(let i=0;i<8;i++){const f=require('./flow'+i+'.js').process;r.push(await f(async x=>x,3));r.push(await f(async()=>{throw Error('denied')},3))}console.log(JSON.stringify(r))})().catch(()=>process.exit(1))"
    result=subprocess.run(['/usr/local/bin/node','-e',program],capture_output=True,text=True);assert result.returncode==0
    output=json.loads(result.stdout)
    for i in range(8):assert output[i*2]==3*(i+1)+1 and output[i*2+1]=={'error':'denied'}
""",
        ["test_no_promise_chains", "test_success_and_failure"],
    )

    initial = {
        "package.json": '{"type":"module","scripts":{"build":"webpack"},"devDependencies":{"webpack":"4.47.0"}}',
        "webpack.config.js": 'export default {entry:"./src/main.js",output:{path:"dist"}};\n',
        "index.html": '<html><body><div id="app"></div><script src="/bundle.js"></script></body></html>',
        "src/main.js": 'document.getElementById("app").textContent=process.env.APP_LABEL;\n',
        ".env": "VITE_APP_LABEL=Pilot shop\n",
        "public/logo.txt": "pilot asset\n",
    }
    reference = {
        "package.json": '{"type":"module","scripts":{"build":"vite build","dev":"vite"},"devDependencies":{"vite":"7.3.6"}}',
        "webpack.config.js": "// Webpack configuration removed from build path.\n",
        "vite.config.js": 'export default {base:"/shop/"};\n',
        "index.html": '<html><body><div id="app"></div><script type="module" src="/src/main.js"></script></body></html>',
        "src/main.js": 'document.getElementById("app").textContent=import.meta.env.VITE_APP_LABEL;\n',
    }
    vite = str(node_modules / "vite/bin/vite.js")
    add(
        "B1",
        "migration",
        "迁移构建和环境变量并验证静态资源",
        "将 Webpack4 项目迁移为 Vite7，构建和 dev 均可运行，base 为 /shop/。环境变量使用 VITE_APP_LABEL，页面显示 Pilot shop；保留 public/logo.txt。离线 Vite 库位于 "
        + str(node_modules)
        + "。",
        initial,
        reference,
        f"""import json,subprocess,time,urllib.request,os,signal
from pathlib import Path
VITE={vite!r}
def test_build_and_environment():
    result=subprocess.run(['/usr/local/bin/node',VITE,'build','--configLoader','native','--outDir','.tmp/build'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    output=''.join(p.read_text() for p in Path('.tmp/build/assets').glob('*.js'))
    assert 'Pilot shop' in output and 'process.env.APP_LABEL' not in output
    dom_program="const vm=require('vm');let element={{textContent:''}};vm.runInNewContext(process.argv[1],{{document:{{createElement:()=>({{relList:{{supports:()=>true}}}}),getElementById:()=>element}}}});if(element.textContent!=='Pilot shop')process.exit(1)"
    rendered=subprocess.run(['/usr/local/bin/node','-e',dom_program,output],capture_output=True,text=True)
    assert rendered.returncode==0,rendered.stderr
    assert Path('.tmp/build/logo.txt').read_text()=='pilot asset\\n'
def test_vite_configuration():
    manifest=json.loads(Path('package.json').read_text());assert manifest['scripts']['build']=='vite build' and 'webpack' not in manifest.get('devDependencies',{{}})
    assert 'type="module"' in Path('index.html').read_text()
def test_dev_page_and_asset():
    process=subprocess.Popen(['/usr/local/bin/node',VITE,'--configLoader','native','--host','127.0.0.1','--port',os.environ.get('GK_TASK_PORT','34101'),'--strictPort'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        for i in range(40):
            try:
                page=urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GK_TASK_PORT','34101')+'/shop/',timeout=.5).read().decode();break
            except Exception:time.sleep(.05)
        else:raise AssertionError('dev server did not start')
        assert '/src/main.js' in page
        assert urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GK_TASK_PORT','34101')+'/shop/logo.txt').read()==b'pilot asset\\n'
    finally:
        os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=5)
""",
        ["test_build_and_environment", "test_vite_configuration", "test_dev_page_and_asset"],
        ports=[34101],
    )

    initial = {
        "schema.py": 'from pydantic import BaseModel,validator\nclass Item(BaseModel):\n    name:str\n    count:int=1\n    @validator("name")\n    def clean(cls,v):return v.strip()\n    class Config:\n        extra="forbid"\ndef dump(data):return Item(**data).dict()\n',
        "requirements.txt": "pydantic==1.10.15\n",
    }
    reference = {
        "schema.py": 'from pydantic import BaseModel,field_validator,ConfigDict\nclass Item(BaseModel):\n    name:str\n    count:int=1\n    model_config=ConfigDict(extra="forbid")\n    @field_validator("name")\n    @classmethod\n    def clean(cls,v):return v.strip()\ndef dump(data):return Item(**data).model_dump()\n',
        "requirements.txt": "pydantic>=2.9,<3\n",
    }
    add(
        "B2",
        "migration",
        "迁移模型校验和序列化至 Pydantic 2",
        "迁移 schema.py 和依赖为 Pydantic2；保留名称去空格、默认数量、额外字段拒绝和序列化结果，不能留下弃用告警。",
        initial,
        reference,
        """import importlib,warnings
from pathlib import Path
import pytest
def test_validation_and_serialization():
    from schema import dump
    assert dump({'name':'  widget  '})=={'name':'widget','count':1}
    with pytest.raises(Exception):dump({'name':'x','extra':1})
def test_no_deprecated_apis():
    text=Path('schema.py').read_text();assert '.dict(' not in text and 'class Config' not in text
    with warnings.catch_warnings():
        warnings.simplefilter('error');import schema;importlib.reload(schema);schema.dump({'name':'x'})
def test_dependency_upgrade():
    assert '1.10' not in Path('requirements.txt').read_text() and '2' in Path('requirements.txt').read_text()
""",
        ["test_validation_and_serialization", "test_no_deprecated_apis", "test_dependency_upgrade"],
    )

    python312 = shutil.which("python3.12")
    initial = {
        "compat.py": "from distutils.version import LooseVersion\nfrom imp import new_module\ndef newer(a,b):return LooseVersion(a)>LooseVersion(b)\ndef make(name):return new_module(name).__name__\n"
    }
    reference = {
        "compat.py": 'import re,types\ndef parts(v):return tuple(int(x) for x in re.findall(r"\\d+",v))\ndef newer(a,b):return parts(a)>parts(b)\ndef make(name):return types.ModuleType(name).__name__\n'
    }
    add(
        "B3",
        "migration",
        "在 Python 3.12 下替换已移除的标准库",
        "迁移 compat.py 到 Python3.12；清除 imp/distutils，保留数字版本比较和动态模块名称。不得出现 DeprecationWarning。",
        initial,
        reference,
        f"""import subprocess
from pathlib import Path
def test_python312_behavior():
    program="import warnings;warnings.simplefilter('error',DeprecationWarning);from compat import newer,make;assert newer('2.10','2.9');assert not newer('2.9','2.10');assert make('sales')=='sales'"
    result=subprocess.run([{python312!r},'-W','error::DeprecationWarning','-c',program],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
def test_removed_imports():
    text=Path('compat.py').read_text();assert 'from imp ' not in text and 'distutils' not in text
""",
        ["test_python312_behavior", "test_removed_imports"],
    )
    cases[-1].environment_ready = bool(python312)

    initial = {
        "ledger.py": "import time\nclass Ledger:\n    def __init__(self):self.total=0\n    def add(self,value):\n        before=self.total;time.sleep(.0001);self.total=before+value\n",
        "service.py": "from ledger import Ledger\ndef batch(values):\n    from concurrent.futures import ThreadPoolExecutor\n    ledger=Ledger()\n    with ThreadPoolExecutor(8) as pool:list(pool.map(ledger.add,values))\n    return ledger.total\n",
    }
    reference = {
        "ledger.py": "from threading import Lock\nclass Ledger:\n    def __init__(self):self.total=0;self.lock=Lock()\n    def add(self,value):\n        with self.lock:self.total+=value\n",
        "ROOT_CAUSE.md": "读取旧值与写入新值之间没有互斥保护，造成更新丢失。锁覆盖完整的读改写操作。\n",
        "tests/test_regression.py": "from service import batch\ndef test_parallel():\n    for _ in range(50):assert batch(list(range(40)))==780\n",
    }
    add(
        "C1",
        "debug",
        "修复并发记账时丢失更新并加入回归检查",
        "修复 ledger/service 的并发错乱，保持 batch 接口；添加至少50轮并发回归测试，保存 ROOT_CAUSE.md 根因说明。",
        initial,
        reference,
        """from pathlib import Path
def test_fifty_concurrent_rounds():
    from service import batch
    for _ in range(50):assert batch(list(range(40)))==780
def test_original_behavior():
    from service import batch
    assert batch([])==0 and batch([3,-1])==2
def test_regression_and_root_cause():
    assert Path('ROOT_CAUSE.md').stat().st_size>20
    files=list(Path('tests').glob('test*.py'));assert files and any('50' in p.read_text() for p in files)
""",
        ["test_fifty_concurrent_rounds", "test_original_behavior", "test_regression_and_root_cause"],
    )

    initial = {
        "render.py": 'DEFAULT={"prefix":""}\ndef render(value,options=DEFAULT):\n    options["last"]=value\n    return dict(options)\n',
        "routes.py": 'from render import render\ndef default_view(value):return render(value)\ndef named_view(value):return render(value,{"prefix":"named"})\n',
    }
    reference = {
        "render.py": 'def render(value,options=None):\n    config={"prefix":""} if options is None else dict(options)\n    config["last"]=value\n    return config\n',
        "ROOT_CAUSE.md": "可变默认对象与调用者对象被函数原地修改，状态在调用之间泄漏。每次创建独立配置副本。\n",
        "tests/test_regression.py": 'from render import render\ndef test_isolated():\n    for _ in range(20):\n        config={"prefix":"x"};render(1,config);assert config=={"prefix":"x"}\n',
    }
    add(
        "C2",
        "debug",
        "消除渲染配置的状态污染和调用顺序依赖",
        "修复 render/routes 状态污染；默认调用和传入配置互不影响，不修改调用者对象，保存根因及20轮回归测试。",
        initial,
        reference,
        """from pathlib import Path
def test_twenty_orders():
    from routes import default_view,named_view
    from render import render
    for _ in range(20):
        config={'prefix':'x'};result=render(1,config);assert config=={'prefix':'x'}
        result['polluted']=True
        assert default_view(2)=={'prefix':'','last':2}
        assert named_view(3)=={'prefix':'named','last':3}
def test_root_cause_and_regression():
    assert Path('ROOT_CAUSE.md').stat().st_size>20
    assert any('20' in p.read_text() for p in Path('tests').glob('test*.py'))
""",
        ["test_twenty_orders", "test_root_cause_and_regression"],
    )

    cart_initial = {
        "cart.py": "def subtotal(items):\n    raise NotImplementedError\ndef total(items,coupon=0):\n    return subtotal(items)\n"
    }
    cart_reference = {
        "cart.py": 'from decimal import Decimal\ndef subtotal(items):\n    if any(q<0 or p<0 for p,q in items):raise ValueError("negative")\n    return sum((Decimal(str(p))*q for p,q in items),Decimal(0))\ndef total(items,coupon=0):\n    value=subtotal(items)\n    reduction=(value//100)*10\n    return max(Decimal(0),value-reduction-Decimal(str(coupon)))\n'
    }
    probe = CheckSpec(
        name="购物车基础功能",
        argv=[sys.executable, "-c", "from cart import subtotal;assert subtotal([(10,2)])==20"],
    )
    add(
        "D1",
        "feedback",
        "在购物车基础功能完成后追加满减与优惠券",
        "实现 cart.subtotal(items) 与 total(items,coupon=0)。items 是 (单价, 数量) 二元组的列表，例如 [(10,2)]；空列表返回 Decimal(0)。单价使用十进制精确计算，数量和单价不可为负，原有函数入口保留。初始 total 等于 subtotal。业务反馈将在基础购物车可用时给出。",
        cart_initial,
        cart_reference,
        """from decimal import Decimal
import pytest
def test_cart_original_contract():
    from cart import subtotal
    assert subtotal([(0.1,3)])==Decimal('0.3')
    with pytest.raises(ValueError):subtotal([(1,-1)])
def test_discount_boundaries():
    from cart import total
    for value,expected in [(99,99),(100,90),(200,180),(0,0)]:assert total([(value,1)])==expected
    assert total([(100,1)],5)==85 and total([(10,1)],50)==0
""",
        ["test_cart_original_contract", "test_discount_boundaries"],
        feedback=Feedback(
            kind="goal_change",
            probe=probe,
            goal="保留全部购物车约束。追加每满100减10，随后扣除 coupon，最终金额最低为0。subtotal 不受折扣影响。",
        ),
    )

    schema_initial = {
        "storage.py": "def load(row):\n    raise NotImplementedError\n",
        "api.py": "from storage import load\ndef response(row):return load(row)\n",
        "view.py": 'def label(row):return str(row["amount"])\n',
    }
    schema_reference = {
        "storage.py": 'def load(row):\n    return {"id":row["id"],"amount_cents":row.get("amount_cents",round(row.get("amount",0)*100))}\n',
        "api.py": "from storage import load\ndef response(row):return load(row)\n",
        "view.py": 'def label(row):return "{:.2f}".format(row["amount_cents"]/100)\n',
    }
    probe = CheckSpec(
        name="旧结构存储可用",
        argv=[
            sys.executable,
            "-c",
            'from storage import load;assert load({"id":"a","amount":12.5})["id"]=="a"',
        ],
    )
    add(
        "D2",
        "feedback",
        "把金额字段变更同步到存储、接口和显示",
        "实现 storage.load、api.response 和 view.label：原始 row 为 id 和 amount，保留 id，显示金额。存储可用后会收到业务 schema 变更。",
        schema_initial,
        schema_reference,
        """def test_old_data_migration():
    from storage import load
    assert load({'id':'a','amount':12.5})=={'id':'a','amount_cents':1250}
def test_new_schema_consistency():
    from api import response
    from view import label
    row=response({'id':'b','amount_cents':199})
    assert row=={'id':'b','amount_cents':199} and label(row)=='1.99'
""",
        ["test_old_data_migration", "test_new_schema_consistency"],
        feedback=Feedback(
            kind="goal_change",
            probe=probe,
            goal="金额改用 amount_cents 整数。旧 amount 数据由 storage 迁移，API 只输出新字段，view 显示两位小数。保留 id 和旧数据兼容。",
        ),
    )

    for case_id, kind in [("E1", "same_session"), ("E2", "new_session")]:
        initial = {
            "parse.py": "def parse(line):\n    raise NotImplementedError\n",
            "aggregate.py": "def aggregate(lines):\n    raise NotImplementedError\n",
            "export.py": "def export(lines):\n    raise NotImplementedError\n",
        }
        reference = {
            "parse.py": 'def parse(line):\n    category,amount=line.split(",");return category.strip(),int(amount)\n',
            "aggregate.py": "from parse import parse\ndef aggregate(lines):\n    result={}\n    for line in lines:\n        key,value=parse(line);result[key]=result.get(key,0)+value\n    return result\n",
            "export.py": "import json\nfrom aggregate import aggregate\ndef export(lines):return json.dumps(aggregate(lines),sort_keys=True)\n",
        }
        add(
            case_id,
            "resume",
            "中断并恢复账单解析、汇总和导出流程"
            if kind == "same_session"
            else "通过共同交接包在新会话中继续账单处理",
            "实现 parse.parse、aggregate.aggregate、export.export，输入分类与整数金额的 CSV 行，分类去空格，同分类相加，JSON 按分类排序。解析完成后将中断一次。恢复后接着完成，不改动已正确的解析结果。",
            initial,
            reference,
            """import json
def test_parse_contract():
    from parse import parse
    assert parse(' A ,12')==('A',12)
def test_aggregate_and_export():
    from aggregate import aggregate
    from export import export
    lines=['B,4','A,10','B,-2']
    assert aggregate(lines)=={'A':10,'B':2}
    assert export(lines)==json.dumps({'A':10,'B':2},sort_keys=True)
""",
            ["test_parse_contract", "test_aggregate_and_export"],
            feedback=Feedback(
                kind=kind,
                probe=CheckSpec(
                    name="解析可用",
                    argv=[sys.executable, "-c", 'from parse import parse;assert parse(" A ,12")==("A",12)'],
                ),
                handoff="共同交接包：parse.parse 已通过分类去空格和整数解析检查。未完成 aggregate.aggregate 与 export.export。保留现有代码与原目标，先读取文件核对进展。",
            ),
        )

    from .counterexamples import add_constraint_mutants

    add_constraint_mutants(package, cases)
    manifest = {
        "source": "pilot",
        "generated_by": "deterministic_developer_fixture_builder",
        "cases": [c.model_dump() for c in cases],
        "limitations": [
            "这是开发题集，不能视为公开 benchmark。",
            "部分题规模较小，真实长程任务适用性必须由后续未见任务评测。",
            "关键约束反例覆盖由评分器实测记录；真实运行的反馈和恢复另按运行证据核验。",
        ],
    }
    write(package, {"manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2)})
    return manifest
