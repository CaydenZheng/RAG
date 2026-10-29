// Pages-only, explicitly simulated state. No authentication or network client imports.
import type { CreatorResult, ModelCatalog, ModelChoice, SkillItem } from "../api/workspaceClient";
import { PublicClientError } from "../api/errors";

const creator: SkillItem = {id:"skill-creator",name:"skill-creator",description:"预设模拟：将资料整理为技能，不调用模型。",builtin:true,enabled:false,revision:0,version:"simulated-creator"};
const items=new Map<string,SkillItem>([[creator.id,creator]]);
function check(signal:AbortSignal):void {if(signal.aborted)throw new DOMException("Aborted","AbortError");}
export async function readModels(signal:AbortSignal):Promise<ModelCatalog>{check(signal);return {items:[{id:"flash",label:"DeepSeek V4-Flash · 模拟",available:true},{id:"pro",label:"DeepSeek V4-Pro · 模拟",available:true}],defaultModel:"flash"};}
export async function readSkills(signal:AbortSignal):Promise<SkillItem[]>{check(signal);return [...items.values()].map(item=>({...item}));}
export async function readSkill(id:string,signal:AbortSignal):Promise<SkillItem>{check(signal);const item=items.get(id);if(!item)throw new PublicClientError("validation","模拟技能不存在。");return {...item,content:item.content??"这是预设模拟技能，不会执行或访问外部服务。"};}
export async function setSkill(item:SkillItem,enabled:boolean,signal:AbortSignal):Promise<SkillItem>{check(signal);const saved={...item,enabled,revision:item.revision+1};items.set(item.id,saved);return saved;}
export async function deleteSkill(id:string,signal:AbortSignal):Promise<void>{check(signal);if(id==="skill-creator")throw new PublicClientError("validation","内置技能不能删除。");items.delete(id);}
export async function uploadSkill(file:File,signal:AbortSignal):Promise<SkillItem>{check(signal);const item:SkillItem={id:crypto.randomUUID(),name:"模拟上传技能",description:"仅当前页面内存示例；没有后端格式校验或持久保存。",content:"此项仅模拟上传交互，不读取用户文件。",builtin:false,enabled:false,revision:0,version:"simulated-upload"};void file;items.set(item.id,item);return item;}
export async function createSkill(file:File,model:ModelChoice,id:string,signal:AbortSignal,onChunk:(chunk:string)=>void):Promise<CreatorResult>{
  check(signal);void file;void model;void id;
  if(!items.get("skill-creator")?.enabled)throw new PublicClientError("validation","请先启用模拟 skill-creator。");
  const content="---\nschema_version: 1\nname: simulated-writing\ndescription: 预设模拟技能\n---\n先给结论，再说明依据。\n";
  onChunk(content);await new Promise<void>(resolve=>setTimeout(resolve,300));check(signal);
  return {filename:"simulated-writing.md",content,checksum:"simulated-no-server-validation"};
}
