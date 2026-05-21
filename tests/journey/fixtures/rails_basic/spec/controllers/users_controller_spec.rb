require "rails_helper"

RSpec.describe UsersController, type: :controller do
  describe "GET #index" do
    it "returns http success" do
      get :index
      expect(response).to have_http_status(:success)
    end
  end

  describe "POST #create" do
    it "creates a user with valid params" do
      post :create, params: { user: { name: "Alice", email: "alice@example.com" } }
      expect(response).to have_http_status(:created)
    end
  end
end
