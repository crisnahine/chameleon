require "rails_helper"

RSpec.describe Users::CreateUser do
  describe "#call" do
    it "creates a user with valid params" do
      result = described_class.new(name: "Bob", email: "bob@example.com").call
      expect(result).to be_a(User)
      expect(result.persisted?).to be true
    end

    it "returns failure on invalid params" do
      result = described_class.new(name: nil, email: nil).call
      expect(result).to be_failure
    end
  end
end
