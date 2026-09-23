export default function FixturesPage() {
  return (
    <main className="content-page">
      <p className="eyebrow">Administrators only</p>
      <h1>Testnet fixtures</h1>
      <div className="fixture-grid">
        <section className="financial-card">
          <h3>Ledger lane</h3>
          <p>
            Issuer wallets, trust lines, funded sender/recipient wallets, and
            path liquidity are provisioned by the repository fixture script.
          </p>
          <code>scripts/provision_testnet.py</code>
        </section>
        <section className="financial-card">
          <h3>Simulation boundary</h3>
          <p>
            USD, MXN, sanctions outcomes, and local-fiat payout references are
            demo fixtures. No production KYC/AML or bank movement occurs.
          </p>
        </section>
        <section className="financial-card">
          <h3>Custody boundary</h3>
          <p>
            Seeds are stored in Secrets Manager and reachable only by the
            signer role. This browser and the AgentCore Runtime never receive
            them.
          </p>
        </section>
      </div>
    </main>
  );
}
