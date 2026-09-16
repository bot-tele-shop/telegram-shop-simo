const { chromium } = require('playwright');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const fs = require('node:fs');

async function main() {
  const outputs = path.resolve(__dirname, '../../outputs');
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1120 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(pathToFileURL(path.join(outputs, 'shop-preview.html')).href);
    await page.getByRole('button', { name: 'Preview item' }).first().click();
    await page.getByRole('button', { name: 'Review & continue' }).click();
    const continueButton = page.getByRole('button', { name: 'Continue to sample invoice' });
    if (await continueButton.isEnabled()) throw new Error('Demo consent gate missing');
    await page.getByLabel('I understand and accept these demo terms.').check();
    await continueButton.click();
    await page.getByRole('button', { name: 'Simulate successful payment' }).click();
    await page.getByText('Demo payment confirmed.', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'View demo orders' }).last().click();
    await page.getByRole('button', { name: 'Reopen the same demo delivery' }).click();
    await page.getByText('No new inventory item is allocated.', { exact: true }).waitFor();
    await page.getByRole('tab', { name: 'What’s built' }).click();
    await page.getByRole('heading', { name: 'Tested beyond the happy path.' }).waitFor();
    await page.getByRole('tab', { name: 'Connect & launch' }).click();
    await page.getByRole('heading', { name: 'Before going live' }).waitFor();
    await page.getByRole('tab', { name: 'Try the storefront' }).click();
    await page.getByRole('button', { name: 'Reset demo' }).click();
    await page.screenshot({ path: path.join(outputs, 'shop-preview-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) {
      throw new Error('Mobile horizontal overflow');
    }
    await page.screenshot({ path: path.join(outputs, 'shop-preview-mobile.png'), fullPage: true });
    if (errors.length) throw new Error(errors.join('\n'));
    const result = {
      page_errors: errors, desktop_viewport: '1440x1120', mobile_viewport: '390x844',
      consent_gate: 'passed', simulated_checkout: 'passed', delivery_reopen: 'passed',
      navigation_tabs: 'passed', mobile_horizontal_overflow: false,
      note: 'Browser-only illustrative preview; not a live Telegram payment test.'
    };
    fs.writeFileSync(path.join(outputs, 'preview-check-result.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result, null, 2));
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
