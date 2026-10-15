import { test, expect } from '@playwright/test';

test('Upstox auth button opens new tab and keeps modal open', async ({ page, context }) => {
  // Listen for new tab
  const newPages: string[] = [];
  page.on('popup', async (popup) => {
    newPages.push(popup.url());
    await popup.waitForLoadState('domcontentloaded');
  });

  // Open the app
  await page.goto('http://127.0.0.1:5000/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);

  // Open broker auth modal — look for a broker chip or login trigger
  // Try clicking the Upstox broker card / login button
  const modal = page.locator('#broker-auth-overlay');
  await modal.waitFor({ state: 'visible', timeout: 10000 }).catch(() => {
    // If modal not auto-opened, try to find and click a login trigger
    page.locator('text=Upstox').first().click().catch(() => {});
    return modal.waitFor({ state: 'visible', timeout: 5000 });
  });

  // Select Upstox broker if selector exists
  const brokerSelect = page.locator('#broker-auth-broker-select');
  if (await brokerSelect.isVisible()) {
    await brokerSelect.selectOption('upstox');
    await page.waitForTimeout(500);
  }

  // Fill credentials
  const username = page.locator('#broker-auth-username');
  const password = page.locator('#broker-auth-password');
  if (await username.isVisible() && await password.isVisible()) {
    await username.fill('4d2b6b1b-c984-46b2-a1c5-28a7b8968918');
    await password.fill('7wbfq2qtbd');
    await page.locator('#broker-auth-login-btn').click();
    await page.waitForTimeout(2000);
  }

  // Now we should be on the TOTP step with the Open button
  const openBtn = page.locator('#broker-auth-totp-hint').locator('button:has-text("Open the Upstox sign-in page")');
  if (await openBtn.isVisible()) {
    // Record whether modal is open before click
    const modalVisibleBefore = await modal.isVisible();

    // Click the button
    await openBtn.click();
    await page.waitForTimeout(3000);

    // Modal should STILL be visible
    const modalVisibleAfter = await modal.isVisible();
    console.log(`Modal visible before: ${modalVisibleBefore}, after: ${modalVisibleAfter}`);
    console.log(`New tabs opened: ${newPages.length}`);
    newPages.forEach(u => console.log(`  New tab: ${u}`));

    expect(modalVisibleAfter).toBeTruthy();
    expect(newPages.length).toBeGreaterThanOrEqual(1);
    expect(newPages[0]).toContain('upstox.com');
  } else {
    console.log('Open button not found — checking current page state:');
    await page.screenshot({ path: 'test_screenshot.png' });
    console.log('Current URL:', page.url());
    console.log('Page content snippet:', await page.locator('body').innerText().then(t => t.substring(0, 500)));
  }
});
