require "rails_helper"

RSpec.describe Posts::PublishPost do
  describe "#call" do
    it "publishes an unpublished post" do
      post = create(:post, status: "draft")
      described_class.new(post).call
      expect(post.reload.status).to eq("published")
    end

    it "returns failure if already published" do
      post = create(:post, status: "published", published_at: 1.day.ago)
      result = described_class.new(post).call
      expect(result).to be_failure
    end
  end
end
