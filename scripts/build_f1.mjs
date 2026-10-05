import os from 'node:os';
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {Workbook,SpreadsheetFile,Presentation,PresentationFile,FileBlob} from '@oai/artifact-tool';
const root=process.env.GK_PROJECT;
const out=process.env.GK_F1_OUTPUT;
const build=process.env.GK_F1_BUILD;
const skill=path.join(os.homedir(),'.codex/plugins/cache/openai-primary-runtime/presentations/26.905.11957/skills/presentations');
const runtime=path.join(os.homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies');
const {finalizePresentation,resolvePresentationFont,applyPresentationChartFont}=await import(pathToFileURL(path.join(skill,'container_tools/artifact_tool_utils.mjs')).href);
const font=resolvePresentationFont({fontFamily:'Arial Unicode MS',availableFonts:['Arial Unicode MS']});
await fs.mkdir(out,{recursive:true});
const source=[];
for(let m=1;m<=6;m++)for(let p=0;p<3;p++)source.push([`${m}月`,['基础款','标准款','专业款'][p],70+m*10+p*15,[80,120,200][p],(m+p)*20]);
await fs.writeFile(path.join(out,'sales-simulated.csv'),'月份,产品,数量,单价（元）,退款（元）\n'+source.map(r=>r.join(',')).join('\n')+'\n');
const wb=Workbook.create();
const summary=wb.worksheets.add('销售分析');
const raw=wb.worksheets.add('原始销售数据');
for(const s of [summary,raw]){s.showGridLines=false;s.getRange('A1:N26').format.font={name:font,size:11,color:'#253C34'};s.getRange('A1:N26').format.rowHeight=24;s.getRange('A1:N26').format.verticalAlignment='center';s.getRange('A1:N26').format.columnWidth=16;}
summary.tabColor='#254E38';
summary.getRange('A2').values=[['上半年销售分析（模拟数据）']];summary.getRange('A2').format.font={name:font,size:16,bold:true};summary.getRange('A3').values=[['金额单位：元；数据来源：sales-simulated.csv，仅用于开发演示。']];summary.getRange('A3').format.font={name:font,size:11,italic:true,color:'#68766C'};
raw.getRange('A1:G1').values=[['月份','产品','销售数量','单价（元）','退款（元）','销售额（元）','净销售额（元）']];
raw.getRange('A2:E19').values=source;
raw.getRange('F2:G2').formulas=[['=C2*D2','=F2-E2']];raw.getRange('F2:G19').fillDown();raw.getRange('D2:G19').setNumberFormat('#,##0.00');raw.getRange('C2:C19').setNumberFormat('0');raw.getRange('A21').values=[['来源：sales-simulated.csv；蓝字输入可编辑，计算列随之更新。']];raw.getRange('A2:E19').format.font={name:font,size:11,color:'#215CB0'};raw.freezePanes.freezeRows(1);
summary.getRange('A5:D5').values=[['月份','销售额（元）','退款（元）','净销售额（元）']];
summary.getRange('A6:A11').values=Array.from({length:6},(_,i)=>[`${i+1}月`]);
summary.getRange('B6:D6').formulas=[["=SUMIFS('原始销售数据'!$F$2:$F$19,'原始销售数据'!$A$2:$A$19,A6)","=SUMIFS('原始销售数据'!$E$2:$E$19,'原始销售数据'!$A$2:$A$19,A6)",'=B6-C6']];summary.getRange('B6:D11').fillDown();summary.getRange('A12').values=[['合计']];summary.getRange('B12:D12').formulas=[['=SUM(B6:B11)','=SUM(C6:C11)','=SUM(D6:D11)']];summary.getRange('B6:D12').setNumberFormat('#,##0.00');
summary.getRange('A15:B15').values=[['产品','净销售额（元）']];summary.getRange('A16:A18').values=[['基础款'],['标准款'],['专业款']];summary.getRange('B16').formulas=[["=SUMIFS('原始销售数据'!$G$2:$G$19,'原始销售数据'!$B$2:$B$19,A16)"]];summary.getRange('B16:B18').fillDown();summary.getRange('B16:B18').setNumberFormat('#,##0.00');
for(const r of [raw.getRange('A1:G1'),summary.getRange('A5:D5'),summary.getRange('A15:B15')]){r.format.fill='#254E38';r.format.font={name:font,size:11,bold:true,color:'#FFFFFF'};r.format.horizontalAlignment='center';}
summary.getRange('A12:D12').format.borders={top:{style:'thin',color:'#254E38'}};summary.getRange('A12:D12').format.font={name:font,size:11,bold:true};
const xc=summary.charts.add('line',[summary.getRange('A5:A11'),summary.getRange('D5:D11')]);xc.title='每月净销售额（元）';xc.hasLegend=false;xc.titleTextStyle.typeface=font;xc.titleTextStyle.fontSize=15;xc.xAxis={axisType:'textAxis',textStyle:{typeface:font,fontSize:13}};xc.yAxis={numberFormatCode:'#,##0',numberFormatSourceLinked:false,textStyle:{typeface:font,fontSize:13}};xc.setPosition('F5','N20');
wb.recalculate();
const total=summary.getRange('D12').values[0][0];const expected=source.reduce((a,r)=>a+r[2]*r[3]-r[4],0);if(total!==expected)throw Error(`total ${total} != ${expected}`);
const original=raw.getRange('C2').values[0][0];raw.getRange('C2').values=[[original+1]];wb.recalculate();if(summary.getRange('D12').values[0][0]!==total+80)throw Error('Input edit failed');raw.getRange('C2').values=[[original]];wb.recalculate();
const scan=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#N/A|#NAME\\?|#NUM!',options:{useRegex:true,maxResults:30},maxChars:3000});await fs.writeFile(path.join(build,'formula-inspect.json'),JSON.stringify(scan));
await(await SpreadsheetFile.exportXlsx(wb)).save(path.join(out,'销售分析.xlsx'));
for(const s of [summary,raw]){const png=await wb.render({sheetName:s.name,range:s===summary?'A1:N22':'A1:H22',scale:1.4,format:'png'});await fs.writeFile(path.join(build,`sheet-${s.name}.png`),new Uint8Array(await png.arrayBuffer()));}
const months=summary.getRange('A6:A11').values.flat();const net=summary.getRange('D6:D11').values.flat();const products=summary.getRange('A16:A18').values.flat();const mix=summary.getRange('B16:B18').values.flat();const growth=(net[5]/net[0]-1)*100;
const refunds=summary.getRange('C6:C11').values.flat();
const money=v=>v.toLocaleString('zh-CN',{maximumFractionDigits:0});
function text(slide,value,y=58,size=44,h=80){const box=slide.shapes.add({geometry:'textbox',position:{left:72,top:y,width:1136,height:h},fill:'none',line:{fill:'none',width:0}});box.text=value;box.text.style={typeface:font,fontSize:size,bold:y===58,color:'#253C34',autoFit:'none'};return box;}
function chart(slide,categories,values,type='line',seriesName='净销售额（元）'){const c=slide.charts.add(type,{position:{left:82,top:185,width:1090,height:400},categories,series:[{name:seriesName,values,fill:'#35744E',valuesFormatCode:'#,##0'}],hasLegend:false,barOptions:{direction:'column',grouping:'clustered'},xAxis:{textStyle:{typeface:font,fontSize:25}},yAxis:{numberFormatCode:'#,##0',numberFormatSourceLinked:false,textStyle:{typeface:font,fontSize:24}},dataLabels:{showValue:true,position:'outEnd',textStyle:{typeface:font,fontSize:24}}});applyPresentationChartFont(c,{fontFamily:font});}
async function deck(revision){const p=Presentation.create({slideSize:{width:1280,height:720}});const slides=[];const notes=`开发演示，全部为模拟数据。来源：sales-simulated.csv；销售分析.xlsx。净销售额=数量×单价−退款，总额=${total}元；月末相比月初增幅=${growth.toFixed(1)}%。图表内含计算后数值快照，不与外部 Excel 自动联动。`;
 const add=(title)=>{const s=p.slides.add();s.background.fill='#FFFFFF';text(s,title);text(s,'模拟销售数据 · 金额单位：元',648,23,36);s.speakerNotes.textFrame.setText(notes);slides.push(s);return s;};
 let s=add('上半年净销售额为 '+money(total)+' 元');text(s,`6 月净销售额为 ${money(net[5])} 元，较 1 月增长 ${growth.toFixed(1)}%。`,revision?142:195,30,90);if(revision)chart(s,months,net);else{text(s,'退款已从销售额中扣除。\n所有数值来自同一份可编辑销售表。',320,30,120);}
 s=add(revision?'退款从 120 元增至 420 元':'各月净销售额持续增加');if(revision)chart(s,months,refunds,'bar','退款（元）');else chart(s,months,net);
 if(!revision){s=add('专业款的净销售额最高');chart(s,products,mix,'bar');s=add('净销售额按销售额减退款计算');text(s,`销售额合计：${money(source.reduce((a,r)=>a+r[2]*r[3],0))} 元\n退款合计：${money(source.reduce((a,r)=>a+r[4],0))} 元\n净销售额合计：${money(total)} 元`,195,35,230);s=add('修改销售表后，分析会自动更新');text(s,'原始销售数据保存数量、单价和退款。\n销售额与净销售额使用公式计算。\n汇报图表采用导出时的数值，更新数据后需重新生成。',195,30,250);s=add('演示文件可以继续编辑');text(s,'Excel：输入、公式和图表可编辑。\n汇报材料：文字和原生图表可编辑。\n当前记录只验证文件流程，尚未执行真实 Agent 任务。',195,30,250);}else{s=add('专业款贡献最高，退款已计入');chart(s,products,mix,'bar');}
 const name=revision?'销售汇报-三页改版.pptx':'销售汇报-六页初版.pptx';const candidate=path.join(build,'draft-'+name);await(await PresentationFile.exportPptx(p)).save(candidate);await finalizePresentation({workspaceDir:root,candidatePath:candidate,finalPath:path.join(out,name),pythonExecutable:runtime+'/python/bin/python3',integrityValidatorPath:skill+'/container_tools/inspect_presentation_package_integrity.py',layoutValidatorPath:skill+'/container_tools/inspect_presentation_layout_geometry.py',layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-heading-fit'],explicitTotalSlideCount:revision?3:6,requiredNativeChartOwnerSlides:revision?[1,2,3]:[2,3],requiredNativeTableOwnerSlides:[],fontPolicy:{basis:'design',families:[font]},materializeLiteralChartWorkbooks:true,verifyArtifactToolImport:true,receiptPath:path.join(build,name+'.validation.json')});
 const final=await PresentationFile.importPptx(await FileBlob.load(path.join(out,name)));for(let i=0;i<slides.length;i++){const png=await final.export({slide:final.slides.items[i],format:'png',scale:1});await fs.writeFile(path.join(build,`${revision?'revision':'initial'}-${i+1}.png`),new Uint8Array(await png.arrayBuffer()));}
 return name;
}
const decks=[await deck(false),await deck(true)];
await fs.writeFile(path.join(build,'metrics.json'),JSON.stringify({total,months,net,products,mix,growth,input_edit_verified:true,decks},null,2));
console.log(JSON.stringify({output:out,build,metrics:{total,growth},real_agent_run:false}));
