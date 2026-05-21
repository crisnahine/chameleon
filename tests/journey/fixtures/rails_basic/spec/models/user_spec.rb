require "rails_helper"

RSpec.describe User, type: :model do
  it "validates presence of email" do
    user = User.new(email: nil, name: "x")
    expect(user).not_to be_valid
  end
end
