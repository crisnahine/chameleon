require "rails_helper"

RSpec.describe Comment, type: :model do
  it "validates presence of body" do
    comment = Comment.new(body: nil)
    expect(comment).not_to be_valid
  end

  it "enforces max length on body" do
    comment = Comment.new(body: "x" * 2001)
    expect(comment).not_to be_valid
  end
end
