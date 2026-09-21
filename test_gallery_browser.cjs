// Run with NODE_PATH pointing at an installation containing playwright.
const { chromium } = require('playwright');
const fs = require('fs'), os = require('os'), path = require('path'), assert = require('assert');
(async () => {
 const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'snapchat-gallery-test-'));
 const items=Array.from({length:205},(_,i)=>({path:`Media/${String(i).padStart(3,'0')}.jpg`,actual:'missing.jpg',type:i%2?'video':'photo',size:(i+1)*1e6,date:i===204?'':`2020-01-${String(i%28+1).padStart(2,'0')} 12:00:00`,sources:[`original-${i}.jpg`],sha256:'hash'+i,refs:i===0?68:1,removed:i===203}));
 const file=path.join(tmp,'gallery.html');fs.writeFileSync(file,fs.readFileSync(path.join(__dirname,'gallery.html'),'utf8').replace('__GALLERY_DATA__',JSON.stringify({library_id:'fixture',items})));
 const browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 try {
 const page=await browser.newPage({viewport:{width:1400,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('file://'+file);await page.evaluate(()=>{localStorage.clear();localStorage.setItem('snapchat-removal-selection-v1',JSON.stringify(['Media/001.jpg']))});await page.reload();
 assert.equal(await page.locator('.card').count(),100);assert.match(await page.locator('#marked-summary').textContent(),/^1 marked/);
 const first=await page.locator('.card').first().getAttribute('data-path');await page.reload();assert.equal(await page.locator('.card').first().getAttribute('data-path'),first);
 await page.locator('#next').click();assert.equal(await page.locator('.card').count(),100);await page.locator('#next').click();assert.equal(await page.locator('.card').count(),4);
 await page.locator('#reset').click();await page.locator('#search').fill('original-204.jpg');await page.waitForTimeout(220);assert.equal(await page.locator('.card').count(),1);
 await page.locator('#reset').click();await page.locator('#dates').selectOption('undated');assert.equal(await page.locator('.card').count(),1);
 await page.locator('#reset').click();await page.locator('#from').fill('2020-01-02');await page.locator('#to').fill('2020-01-02');assert.equal(await page.locator('.card').count(),8);
 await page.locator('#type').selectOption('video');await page.locator('#min').fill('100');await page.locator('#min').blur();assert.equal(await page.locator('.card').count(),4);
 await page.locator('#reset').click();await page.locator('#sort').selectOption('smallest');assert.equal(await page.locator('.card').first().getAttribute('data-path'),'Media/000.jpg');
 await page.locator('#sort').selectOption('largest');assert.equal(await page.locator('.card').first().getAttribute('data-path'),'Media/204.jpg');
 await page.locator('[data-preset=duplicates]').click();assert.equal(await page.locator('.card').count(),1);assert.match(await page.locator('.card').textContent(),/68 export references/);assert.match(await page.locator('#matches').textContent(),/1 matching files · 1.0 MB/);
 await page.locator('#mark-page').click();await page.locator('#reset').click();assert.match(await page.locator('#marked-summary').textContent(),/^2 marked/);await page.reload();assert.match(await page.locator('#marked-summary').textContent(),/^2 marked/);
 await page.locator('[data-preset=marked]').click();assert.equal(await page.locator('.card').count(),2);await page.locator('#unmark-page').click();assert.equal(await page.locator('.card').count(),0);
 await page.locator('#reset').click();await page.locator('#mark-page').click();assert.match(await page.locator('#marked-summary').textContent(),/^100 marked/);
 const [download]=await Promise.all([page.waitForEvent('download'),page.locator('#export').click()]);const exported=JSON.parse(fs.readFileSync(await download.path(),'utf8'));assert.equal(exported.items.length,100);assert.equal(exported.format,'snapchat-removal-selection-v2');
 page.once('dialog',d=>d.dismiss());await page.locator('#clear').click();assert.match(await page.locator('#marked-summary').textContent(),/^100 marked/);
 page.once('dialog',d=>d.accept());await page.locator('#clear').click();assert.match(await page.locator('#marked-summary').textContent(),/^0 marked/);
 for(const version of [1,2]){await page.locator('#import').setInputFiles({name:'selection.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({format:'snapchat-removal-selection-v'+version,[version===1?'videos':'items']:[{path:'Media/001.jpg',sha256:'hash1'}]}))});await page.waitForTimeout(100);assert.match(await page.locator('#marked-summary').textContent(),/^1 marked/)}
 page.once('dialog',d=>d.accept());await page.locator('#import').setInputFiles({name:'bad.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({format:'snapchat-removal-selection-v2',items:[{path:'Media/001.jpg',sha256:'wrong'}]}))});await page.waitForTimeout(100);assert.match(await page.locator('#marked-summary').textContent(),/^1 marked/);
 await page.locator('#view').selectOption('removed');assert.equal(await page.locator('.card').count(),1);assert.equal(await page.locator('.card button').count(),0);await page.locator('#reset').click();await page.locator('#group').selectOption('day');assert.ok(await page.locator('.day').count()>0);
 await page.locator('#reset').click();await page.waitForTimeout(100);assert.ok(await page.locator('img[src],video[src]').count()<30);assert.equal(await page.locator('img').first().evaluate(e=>getComputedStyle(e).objectFit),'contain');
 await page.evaluate(()=>localStorage.setItem('snapchat-gallery:fixture:marks','invalid json'));await page.reload();assert.equal(await page.locator('#storage-note').isVisible(),true);
 await page.addInitScript(()=>{Storage.prototype.setItem=function(){throw Error('blocked')};Storage.prototype.getItem=function(){throw Error('blocked')}});await page.reload();await page.locator('#mark-page').click();assert.match(await page.locator('#marked-summary').textContent(),/^100 marked/);assert.equal(await page.locator('#storage-note').isVisible(),true);
 assert.deepEqual(errors,[]);console.log('Browser fixtures passed: filters, dates, sizes, sorting, pagination, duplicates, marks, import/export, migration, storage failures and bounded previews.');
 } finally {await browser.close();fs.rmSync(tmp,{recursive:true,force:true})}
})().catch(e=>{console.error(e);process.exitCode=1});
