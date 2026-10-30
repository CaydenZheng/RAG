import { expect, test, type BrowserContext, type Page } from "@playwright/test";

// Browser interaction evidence only. Real auth/owner/SQLite contracts are tested
// separately in Python; no credentials or Provider are contacted by this fixture.
interface Entry {
  id: string; mode: string; title: string; revision: number; epoch: number;
  updated_at: string; turns: Record<string, unknown>[];
  model?: string;
}

function fixture(): (context: BrowserContext) => Promise<void> {
  const records = new Map<string, Entry>();
  return async (context) => {
    let identity = "";
    await context.route(/\/(workspace\/(config|models)|auth\/|conversations|ready|query\/stream|agent\/(chat|approvals|elicitation))/, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const reply = async (body: unknown, status = 200): Promise<void> => route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
      const account = (): Record<string, unknown> => ({user:{id:identity,username:identity,role:"member",must_change_password:false},csrf_token:"f".repeat(64)});
      if (path === "/workspace/config") return reply({auth_enabled:true});
      if (path === "/auth/login") { identity = request.postDataJSON().username; return reply(account()); }
      if (path === "/auth/me") return reply(identity ? account() : {},identity ? 200 : 401);
      if (path === "/auth/logout") { identity=""; await route.fulfill({status:204}); return; }
      if (!identity) return reply({},401);
      if (path === "/workspace/models") return reply({items:[{id:"flash",label:"DeepSeek V4-Flash",available:true},{id:"pro",label:"DeepSeek V4-Pro",available:true}],default_model:"flash"});
      if (path === "/ready") return reply({status:"ready"});
      if (/\/agent\/(approvals|elicitation)\//.test(path)) return reply({pending:[]});
      const prefix = `${identity}:`;
      const body = request.method() === "GET" ? null : request.postDataJSON();
      if (path === "/conversations") {
        if (request.method() === "POST") {
          const entry: Entry = {id:body.id,mode:body.mode,model:body.model??"flash",title:"",revision:0,epoch:0,updated_at:new Date().toISOString(),turns:[]};
          records.set(prefix+body.id,entry); return reply(entry,201);
        }
        return reply({items:[...records.entries()].filter(([key,item]) => key.startsWith(prefix) && item.mode === url.searchParams.get("mode")).map(([,entry]) => entry).reverse(),next_cursor:null});
      }
      const matching = path.match(/^\/conversations\/([^/]+)(\/turns|\/clear)?$/);
      if (matching) {
        const entry = records.get(prefix+matching[1]);
        if (!entry) return reply({},404);
        if (matching[2] === "/turns") return reply({items:entry.turns,next_before:null});
        if (request.method() === "DELETE") {records.delete(prefix+matching[1]);await route.fulfill({status:204});return;}
        if (matching[2] === "/clear") { entry.turns=[];entry.epoch+=1;entry.revision+=1; }
        if (request.method() === "PATCH") {if(body.title!==undefined)entry.title=body.title;if(body.model)entry.model=body.model;entry.revision+=1;}
        return reply(entry);
      }
      if (path === "/query/stream" || path === "/agent/chat/stream") {
        const question = path === "/query/stream" ? url.searchParams.get("query") : body.message;
        const id = path === "/query/stream" ? url.searchParams.get("conversation_id") : body.conversation_id;
        const entry = records.get(prefix+id);
        if (!entry) return reply({},404);
        if (question === "delayed private question") await new Promise((resolve) => setTimeout(resolve,1000));
        const answer = `Synthetic answer for ${identity}`;
        const clientTurnId = path === "/query/stream" ? url.searchParams.get("client_turn_id") : body.client_turn_id;
        entry.title ||= question; entry.revision+=1;
        const metadata = {requested_model:entry.model,used_model:`fixture-${entry.model}`,skills:[]};
        entry.turns.push({seq:entry.turns.length+1,question,answer,client_turn_id:clientTurnId,sources:[],index_version:"fixture",...metadata});
        const chunk = path === "/query/stream" ? {event:"chunk",done:false,chunk:answer} : {chunk:answer};
        const done = path === "/query/stream" ? {event:"done",done:true,answer,sources:[],index_version:"fixture",...metadata} : {done:true,answer,iterations:1,error:"",error_code:"",...metadata};
        await route.fulfill({contentType:"text/event-stream",body:`data: ${JSON.stringify(chunk)}\n\ndata: ${JSON.stringify(done)}\n\n`}).catch(() => undefined);
        return;
      }
      await route.fallback();
    });
  };
}

async function login(page: Page, user = "alice"): Promise<void> {
  await page.goto("/");
  await page.getByLabel("账号",{exact:true}).fill(user);
  await page.getByLabel("密码",{exact:true}).fill("synthetic-browser-password");
  await page.getByRole("button",{name:"登录",exact:true}).click();
  await expect(page.getByRole("heading",{name:"知识问答工作台"})).toBeAttached();
  await expect(page.getByRole("main")).toBeFocused();
}

test("fake API: two accounts isolate history; a second device reopens and continues", async ({browser}) => {
  const install = fixture();
  // Cross-device history is sequential, not live collaboration. Release each
  // completed device before allocating another; keep all ownership assertions.
  async function onDevice(user: string, scenario: (page: Page) => Promise<void>): Promise<void> {
    const context = await browser.newContext();
    await install(context);
    try { const page = await context.newPage(); await login(page,user); await scenario(page); }
    finally { await context.close(); }
  }
  let original = "";
  await onDevice("alice", async (page) => {
    await page.getByRole("textbox",{name:/向知识库提问/}).fill("Alice private question");
    await page.getByRole("button",{name:"发送",exact:true}).click();
    await expect(page.getByText("Synthetic answer for alice",{exact:true})).toBeVisible();
    original = page.url();
    await page.getByRole("button",{name:"新建对话",exact:true}).click();
    await expect(page).not.toHaveURL(original);
    await expect(page.getByRole("button",{name:/^Alice private question/})).toBeVisible();
  });
  await onDevice("alice", async (device) => {
    await device.getByRole("button",{name:/^Alice private question/}).click();
    await expect(device.getByText("Synthetic answer for alice",{exact:true})).toBeVisible();
    await device.reload();
    await expect(device.getByText("Synthetic answer for alice",{exact:true})).toBeVisible();
    await device.getByRole("textbox",{name:/向知识库提问/}).fill("Continue on second device");
    await device.getByRole("button",{name:"发送",exact:true}).click();
    await expect(device.getByText("Synthetic answer for alice",{exact:true})).toHaveCount(2);
    await device.getByRole("link",{name:"Agent",exact:true}).click();
    await expect(device.getByRole("heading",{name:"Agent 工作台"})).toBeAttached();
    await device.getByRole("link",{name:"知识问答",exact:true}).click();
    await expect(device).toHaveURL(original);
    await device.reload();
    await expect(device.getByText("Synthetic answer for alice",{exact:true})).toHaveCount(2);
  });
  await onDevice("bob", async (bob) => {
    await bob.goto(original);
    await expect(bob.getByText("会话不存在或不可访问，请刷新目录。",{exact:true})).toBeVisible();
    await expect(bob.getByText("Synthetic answer for alice",{exact:true})).toHaveCount(0);
  });
});

test("fake API: rename, clear and delete are explicit; logout cancels connecting work", async ({context,page}) => {
  await fixture()(context); await login(page);
  await page.getByRole("button",{name:"新建对话",exact:true}).click();
  await page.locator('.history-item[data-active="true"] [aria-haspopup="menu"]').click();
  await page.getByRole("menuitem",{name:"重命名",exact:true}).click();
  await page.getByLabel("会话标题",{exact:true}).fill("Named history");
  await page.getByRole("button",{name:"保存",exact:true}).click();
  await expect(page.getByRole("button",{name:/^Named history/})).toBeVisible();
  page.on("dialog",(dialog) => void dialog.accept());
  await page.locator('.history-item[data-active="true"] [aria-haspopup="menu"]').click();
  await page.getByRole("menuitem",{name:"清空内容",exact:true}).click();
  await expect(page.getByRole("button",{name:/^Named history/})).toBeVisible();
  await page.locator('.history-item[data-active="true"] [aria-haspopup="menu"]').click();
  await page.getByRole("menuitem",{name:"删除会话",exact:true}).click();
  await expect(page.getByRole("button",{name:/^Named history/})).toHaveCount(0);
  await page.getByRole("textbox",{name:/向知识库提问/}).fill("delayed private question");
  await page.getByRole("button",{name:"发送",exact:true}).click();
  await page.getByRole("button",{name:"退出",exact:true}).click();
  await expect(page.getByRole("heading",{name:"登录工作台"})).toBeVisible();
  await expect(page.getByText("delayed private question",{exact:true})).toHaveCount(0);
  await page.getByLabel("账号",{exact:true}).fill("bob");
  await page.getByLabel("密码",{exact:true}).fill("synthetic-browser-password");
  await page.getByRole("button",{name:"登录",exact:true}).click();
  await expect(page.getByRole("heading",{name:"知识问答工作台"})).toBeAttached();
  await expect(page.getByText("delayed private question",{exact:true})).toHaveCount(0);
  const storage = await page.evaluate(() => JSON.stringify(localStorage));
  expect(storage).not.toMatch(/synthetic-browser-password|csrf|private question/);
});

test("fake API: model selection persists on refresh and answer metadata keeps the old model", async ({context,page}) => {
  await fixture()(context); await login(page);
  const model = page.getByRole("combobox", {name:"模型",exact:true});
  await expect(model).toBeEnabled();
  await model.selectOption("pro");
  await page.getByRole("textbox",{name:/向知识库提问/}).fill("Model choice question");
  await page.getByRole("button",{name:"发送",exact:true}).click();
  await expect(page.getByText("实际模型 fixture-pro",{exact:true})).toBeVisible();
  await model.selectOption("flash");
  await expect(model).toHaveValue("flash");
  await page.reload();
  await expect(model).toHaveValue("flash");
  await expect(page.getByText("实际模型 fixture-pro",{exact:true})).toBeVisible();
});

test("fake API: skills route, explicit creator upload and narrow navigation", async ({context,page}) => {
  const {createHash} = await import("node:crypto");
  const content = "---\nschema_version: 1\nname: browser-skill\ndescription: Example\n---\nBe concise.\n";
  const checksum = createHash("sha256").update(content).digest("hex");
  const builtin = {id:"skill-creator",name:"skill-creator",description:"Convert only",builtin:true,enabled:false,revision:0,version:checksum};
  const items = [builtin];
  let uploads=0;
  await fixture()(context);
  await context.route(/^https?:\/\/[^/]+\/skills(?:\/.*)?$/,async route=>{
    const request=route.request();const path=new URL(request.url()).pathname;
    const reply=async(body:unknown,status=200):Promise<void>=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
    if(path==="/skills/creator/stream")return route.fulfill({contentType:"text/event-stream",body:`data: ${JSON.stringify({chunk:content})}\n\ndata: ${JSON.stringify({done:true,filename:"browser-skill.md",content,checksum})}\n\n`});
    if(request.method()==="PATCH"){builtin.enabled=request.postDataJSON().enabled;builtin.revision+=1;return reply(builtin);}
    if(request.method()==="POST"){uploads+=1;const custom={...builtin,id:"custom",name:"browser-skill",builtin:false,enabled:false};items.push(custom);return reply(custom,201);}
    if(request.method()==="DELETE"){items.splice(1);return route.fulfill({status:204});}
    return reply(path==="/skills"?{items}:items.find(item=>path.endsWith(item.id)));
  });
  await login(page);
  await page.getByRole("link",{name:"Skills 技能",exact:true}).click();
  await expect(page).toHaveURL(/\/settings\/skills$/);
  await expect(page.getByRole("main")).toBeFocused();
  const toggle=page.getByRole("checkbox",{name:"启用 skill-creator"});
  await toggle.click();
  await expect(toggle).toBeChecked();
  await page.locator('input[type="file"]').setInputFiles({name:"browser-skill.md",mimeType:"text/markdown",buffer:Buffer.from(content)});
  await expect(page.getByRole("checkbox",{name:"启用 browser-skill"})).not.toBeChecked();
  expect(uploads).toBe(1);
  await page.getByRole("button",{name:"更多操作 browser-skill",exact:true}).click();
  await page.getByRole("menuitem",{name:"详情",exact:true}).click();
  await expect(page.getByRole("region",{name:"技能详情"})).toBeVisible();
  await page.getByRole("button",{name:"关闭详情",exact:true}).click();
  await page.reload();await expect(toggle).toBeChecked();
  await page.setViewportSize({width:768,height:900});
  await page.getByRole("button",{name:"打开导航",exact:true}).click();
  await page.getByRole("link",{name:"Skills 技能",exact:true}).click();
  await expect(page.getByRole("button",{name:"打开导航",exact:true})).toBeFocused();
  await page.getByRole("button",{name:"打开导航",exact:true}).click();
  await page.getByRole("link",{name:"知识问答",exact:true}).click();
  await expect(page.getByRole("main")).toBeFocused();
  await page.goBack();await expect(page.getByRole("heading",{name:"Skills 工作台"})).toBeAttached();
});
