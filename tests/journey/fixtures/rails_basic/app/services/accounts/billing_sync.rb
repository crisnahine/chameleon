module Accounts
  class BillingSync
    def initialize(account)
      @account = account
    end

    def call
      # Sync account billing state with payment provider
      plan_data = fetch_plan_data(@account.stripe_customer_id)
      @account.update!(plan: plan_data[:plan], billing_status: plan_data[:status])
    end

    private

    def fetch_plan_data(customer_id)
      # stubbed; real impl calls Stripe API
      { plan: "pro", status: "active" }
    end
  end
end
