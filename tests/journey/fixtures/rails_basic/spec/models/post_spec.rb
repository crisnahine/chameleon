require "rails_helper"

RSpec.describe Post, type: :model do
  it "validates presence of title" do
    post = Post.new(title: nil, body: "some body", user: build(:user))
    expect(post).not_to be_valid
  end

  it "validates presence of body" do
    post = Post.new(title: "title", body: nil, user: build(:user))
    expect(post).not_to be_valid
  end
end
