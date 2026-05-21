require "rails_helper"

RSpec.describe PostsController, type: :controller do
  describe "GET #index" do
    it "returns http success" do
      get :index
      expect(response).to have_http_status(:success)
    end
  end

  describe "DELETE #destroy" do
    it "deletes a post" do
      post_record = create(:post)
      delete :destroy, params: { id: post_record.id }
      expect(response).to have_http_status(:no_content)
    end
  end
end
