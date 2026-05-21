class SearchController < ApplicationController
  def index
    query = params[:q].to_s.strip
    @results = {
      posts: Post.where("title ILIKE ?", "%#{query}%"),
      users: User.where("name ILIKE ? OR email ILIKE ?", "%#{query}%", "%#{query}%"),
    }
    render json: @results
  end
end
